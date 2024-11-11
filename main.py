from flask import Flask, send_file, request, jsonify, Response
from pytubefix import YouTube
import os
import tempfile
from moviepy.editor import VideoFileClip
import re
from datetime import datetime
import shutil
import json
import threading
from queue import Queue

app = Flask(__name__)

# global queues for progress updates
progress_queues = {}

def fix_video_url(url):
    """Clean and validate YouTube URL"""
    url = url.strip()
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f'https://www.youtube.com/watch?v={video_id}'
    return url

def generate_download_id():
    return datetime.now().strftime('%Y%m%d_%H%M%S')

def on_progress_callback(stream, chunk, bytes_remaining, download_id):
    """callback function to print download progress"""
    if download_id in progress_queues:
        total_size = stream.filesize
        bytes_downloaded = total_size - bytes_remaining
        percentage = (bytes_downloaded / total_size) * 100

        progress_queues[download_id].put({
            'type': 'download',
            'progress': round(percentage, 2)
        })

def process_video(url, start_time, end_time, download_id, progress_queue):
    """process video download and clipping in a separate thread"""

    temp_dir = None
    video_clip = None

    try:
        url = fix_video_url(url)
        temp_dir = tempfile.mkdtemp()

        # initialize yt obj with progress callback
        yt = YouTube(
            url,
            on_progress_callback=lambda stream, chunk, bytes_remaining:
                on_progress_callback(stream, chunk, bytes_remaining, download_id)
        )

        # get video title
        video_title = yt.title
        safe_title = "".join([c for c in video_title if c.isalpha() or c.isdigit() or c==' ']).rstrip()

        # get highest resolution video stream
        streams = yt.streams.filter(progressive=True).order_by('resolution').desc()
        if not streams:
            progress_queue_put({'type': 'error', 'message': 'no suitable video streams found'})
            return

        video = streams[0]

        # download video
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        video_path = video.download(
            output_path=temp_dir,
            filename=f"temp_video_{timestamp}.mp4"
        )

        # create a copy of the file for sending
        final_path = os.path.join(temp_dir, f"final_{timestamp}.mp4")

        # handle clip creation if times are provided
        if start_time is not None and end_time is not None and end_time > start_time:
            progress_queue.put({'type': 'status', 'message': 'creating clip...'})
            video_clip = VideoFileClip(video_path)
            clip = video_clip.subclip(start_time, end_time)

            clip.write_videofile(
                final_path,
                codec='libx264',
                verbose=False
            )
            clip.close()
            video_clip.close()
        else:
            # if not clipping needed, just copy file
            shutil.copy2(video_path, final_path)
        
        # cleanup original file
        try:
            os.unlink(video_path)
        except:
            pass

        # store the path and title for download route
        progress_queue.put({
            'type': 'complete',
            'path': final_path,
            'title': safe_title,
            'timestamp': timestamp
        })
    
    except Exception as e:
        progress_queue.put({'type': 'error', 'message': str(e)})

    finally:
        if video_clip is not None:
            try:
                video_clip.close()
            except:
                pass

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/start-download', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url')
    start_time = data.get('start_time', 0)
    end_time = data.get('end_time', None)

    if not url:
        return jsonify({'error': 'no url provided'}), 400
    
    download_id = generate_download_id()
    progress_queues[download_id] = Queue()

    # start processing in a separate thread
    thread = threading.Thread(
        target=process_video,
        args=(url, start_time, end_time, download_id, progress_queues[download_id])
    )
    thread.daemon = True
    thread.start()

    return jsonify({'download_id': download_id})

@app.route('/progress/<download_id>')
def progress(download_id):
    def generate():
        if download_id not in progress_queues:
            return
        
        queue = progress_queues[download_id]

        while True:
            try:
                data = queue.get(timeout=30)
                if data['type'] == 'complete':
                    # store completion data and cleanup queue
                    app.config[f'download_{download_id}'] = data
                    del progress_queues[download_id]
                yield f'data: {json.dumps(data)}\n\n'
                if data['type'] in ['complete', 'error']:
                    break
            except:
                break
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download/<download_id>')
def download_file(download_id):
    download_data = app.config.get(f'download_{download_id}')
    if not download_data:
        return jsonify({'error': 'download not found'}), 404
    
    try:
        return_data = send_file(
            download_data['path'],
            as_attachment=True,
            download_name=f'{download_data['title']}_{download_data['timestamp']}.mp4',
            mimetype='video/mp4'
        )

        # clean up after sending
        try:
            os.unlink(download_data['path'])
            os.rmdir(os.path.dirname(download_data['path']))
        except:
            pass
            
        del app.config[f'download_{download_id}']
        return return_data
    
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    
if __name__ == '__main__':
    app.run(debug=True)