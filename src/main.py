from flask import Flask, send_file, request, jsonify, Response
from pytubefix import YouTube
import os
import tempfile
import re
import subprocess
from datetime import datetime
import shutil
import json
import threading
from queue import Queue
from pathlib import Path

# define base routes
ROOT_DIR = Path(__file__).parent.parent
FFMPEG_DIR = ROOT_DIR / 'ffmpeg'
DOWNLOADS_DIR = ROOT_DIR / 'downloads'
TEMPLATES_DIR = ROOT_DIR / 'templates'
STATIC_DIR = ROOT_DIR / 'static'

app = Flask(__name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR
)

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

def convert_time_to_seconds(time_str):
    """converting time string 'MM:SS' to seconds"""
    if ':' in time_str:
        minutes, seconds = time_str.split(':')
        return float(minutes) * 60 + float(seconds)
    return float(time_str)

def get_ffmpeg_path():
    """get the ffmpeg executable path"""
    # first check for ffmpeg in the same directory as the script
    ffmpeg_path = FFMPEG_DIR / 'ffmpeg.exe'

    if ffmpeg_path.exists():
        return str(ffmpeg_path)
    
    # if not, check in the system PATH
    if os.system('ffmpeg -version') == 0:
        return 'ffmpeg'

    raise Exception('ffmpeg not found')

def process_video(url, start_time, end_time, download_id, progress_queue):
    """process video download and clipping in a separate thread"""

    temp_dir = None

    try:
        url = fix_video_url(url)
        temp_dir = tempfile.mkdtemp()

        # create output directory
        DOWNLOADS_DIR.mkdir(exist_ok=True)

        # initialize yt obj with progress callback
        yt = YouTube(
            url,
            on_progress_callback=lambda stream, chunk, bytes_remaining:
                on_progress_callback(stream, chunk, bytes_remaining, download_id)
        )

        # get video title
        video_title = yt.title
        safe_title = "".join([c for c in video_title if c.isalpha() or c.isdigit() or c==' ']).rstrip()

        # get highest quality video and lower quality audio
        video_stream = yt.streams.filter(adaptive=True, file_extension='mp4', type='video').order_by('resolution').desc().first()
        audio_stream = yt.streams.filter(adaptive=True, file_extension='mp4', type='audio').order_by('abr').first()

        if not video_stream or not audio_stream:
            progress_queue.put({'type': 'error', 'message': 'no suitable streams found'})
            return

        # Agregar información sobre la calidad seleccionada
        progress_queue.put({
            'type': 'status',
            'message': f'Downloading video: {video_stream.resolution}, audio: {audio_stream.abr}'
        })

        # download video and audio streams
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        video_path = video_stream.download(
            output_path=temp_dir,
            filename=f"temp_video_{timestamp}.mp4"
        )
        audio_path = audio_stream.download(
            output_path=temp_dir,
            filename=f"temp_audio_{timestamp}.mp4"
        )

        # obtain and verify ffmpeg route
        try:
            ffmpeg_path = get_ffmpeg_path()
            progress_queue.put({'type': 'status', 'message': f'using ffmpeg from: {ffmpeg_path}'})
        except Exception as e:
            progress_queue.put({'type': 'error', 'message': str(e)})
            return

        # convert times to float
        if start_time is not None and end_time is not None:
            start_seconds = convert_time_to_seconds(start_time)
            end_seconds = convert_time_to_seconds(end_time)

        # combine video and audio using ffmpeg directly
        progress_queue.put({'type': 'status', 'message': 'combining video and audio...'})
        final_path = os.path.join(temp_dir, f"final_{timestamp}.mp4")

        # if we need to cut video
        if start_time is not None and end_time is not None and end_time > start_time:
            progress_queue.put({
                'type': 'status',
                'message': 'creating clip...'
            })

            duration = end_seconds - start_seconds

            cmd = [
                ffmpeg_path,
                '-i', video_path,
                '-i', audio_path,
                '-ss', str(start_seconds),
                '-t', str(duration),
                '-c:v', 'copy', # copy video without re-encoding
                '-c:a', 'aac', # use aac for audio
                '-b:a', '128k', # set audio bitrate to 128k
                '-y', # overwrite output file if it exists
                final_path
            ]
        else:
            cmd = [
                ffmpeg_path,
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy', # copy video without re-encoding
                '-c:a', 'aac', # use aac for audio
                '-b:a', '128k', # set audio bitrate to 128k
                '-y', # overwrite output file if it exists
                final_path
            ]
        
        # execute ffmpeg command
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        # wait for process to complete
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise Exception(f"Error combining video and audio: {stderr}")
        
        # move file to output directory
        permanent_path = DOWNLOADS_DIR / f"final_{timestamp}.mp4"
        shutil.move(final_path, str(permanent_path))

        try:
            os.unlink(video_path)
            os.unlink(audio_path)
        except Exception as e:
            print(f"Error cleaning up temporary files: str{e}")

        # return success data
        progress_queue.put({
            'type': 'complete',
            'path': str(permanent_path),
            'title': safe_title, 
            'timestamp': timestamp
        })

    except Exception as e:
        progress_queue.put({'type': 'error', 'message': str(e)})

    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                print(f"Error removing temp directory: {str(e)}")

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