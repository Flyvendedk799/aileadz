import os
from flask import Flask, Blueprint, render_template, request, redirect, url_for, flash, send_from_directory
import openai
from werkzeug.utils import secure_filename
from moviepy.editor import VideoFileClip

app4_bp = Blueprint('app4', __name__, template_folder='templates', static_folder='static')

# Hardcoded API key for demonstration (replace with your actual key)
openai.api_key = "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"

# Directory to store uploaded videos (for demo purposes)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Main index page with tabs for all three features
@app4_bp.route('/', methods=['GET'])
def index():
    return render_template('index4.html')

# --- Auto Caption Feature (Updated) ---
@app4_bp.route('/auto_caption', methods=['POST'])
def auto_caption():
    if 'video' not in request.files:
        flash("No video file provided.")
        return redirect(url_for('app4.index'))
    file = request.files['video']
    if file.filename == '':
        flash("No selected file.")
        return redirect(url_for('app4.index'))
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        video_filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(video_filepath)
        
        # Extract audio from video using MoviePy
        try:
            clip = VideoFileClip(video_filepath)
            # Save audio as a WAV file (Whisper works well with WAV)
            audio_filepath = os.path.join(UPLOAD_FOLDER, f"{filename}.wav")
            clip.audio.write_audiofile(audio_filepath, logger=None)
        except Exception as e:
            flash(f"Error extracting audio: {str(e)}")
            return redirect(url_for('app4.index'))
        
        # Use Whisper to transcribe the audio
        try:
            with open(audio_filepath, "rb") as audio_file:
                transcript = openai.Audio.transcribe("whisper-1", audio_file)
            captions = transcript.get("text", "No transcription available.")
        except Exception as e:
            captions = f"Error generating captions: {str(e)}"
        
        # (Optional) Clean up the extracted audio file if desired:
        # os.remove(audio_filepath)
        
        # For now, we simply return the transcribed captions.
        return render_template('auto_caption_result.html', captions=captions, video_filename=filename)
    else:
        flash("Invalid file type.")
        return redirect(url_for('app4.index'))

# Serve uploaded video files
@app4_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# --- Improve Video Feature ---
@app4_bp.route('/improve_video', methods=['POST'])
def improve_video():
    if 'video' not in request.files:
        flash("No video file provided.")
        return redirect(url_for('app4.index'))
    file = request.files['video']
    if file.filename == '':
        flash("No selected file.")
        return redirect(url_for('app4.index'))
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        # Build prompt for video improvement (remove pauses, auto-focus, quality boost)
        prompt = (
            f"Enhance the video '{filename}' by removing long pauses and applying auto-focus and other quality improvements."
        )
        try:
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert video editing assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7
            )
            improvement_details = response.choices[0].message.content
        except Exception as e:
            improvement_details = f"Error improving video: {str(e)}"
        
        return render_template('improve_video_result.html', improvement_details=improvement_details, video_filename=filename)
    else:
        flash("Invalid file type.")
        return redirect(url_for('app4.index'))

# --- Product Ad Feature ---
@app4_bp.route('/product_ad', methods=['POST'])
def product_ad():
    title = request.form.get('title')
    description = request.form.get('description')
    image_url = request.form.get('image_url')
    
    # Build prompt for creating a product advertisement video
    prompt = (
        f"Create an engaging product advertisement video with the following details:\n"
        f"Title: {title}\nDescription: {description}\nImage: {image_url}\n"
        "The video should be visually appealing, persuasive, and optimized for social media."
    )
    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an expert in creating product advertisement videos."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        ad_details = response.choices[0].message.content
    except Exception as e:
        ad_details = f"Error generating product ad video: {str(e)}"
    
    return render_template('product_ad_result.html', ad_details=ad_details)

# --- Standalone Runner for Testing ---
if __name__ == '__main__':
    app = Flask(__name__)
    app.secret_key = 'your_secret_key_here'  # Replace with a secure key
    app.register_blueprint(app4_bp, url_prefix='/app4')
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.run(debug=True)
