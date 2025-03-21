import os
import json
import math
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import timedelta
from flask import Blueprint, render_template, request, redirect, url_for, send_from_directory, flash, current_app
import whisper  # Local Whisper transcription

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app4_bp = Blueprint('app4', __name__, template_folder='templates')

# Configuration constants
DEFAULT_MAX_SEGMENT_DURATION = 1.0
WHISPER_MODEL_SIZE = "base"
VIDEO_PROCESSING_CONFIG = {
    "font_paths": {
        "arial": "C:/Windows/Fonts/arial.ttf",
        "times": "C:/Windows/Fonts/times.ttf",
        "inter": "C:/Windows/Fonts/Inter-Regular.ttf",
        "monospace": "C:/Windows/Fonts/consola.ttf"
    },
    "style_map": {
        "tiktok": "Fontsize=28,MarginV=40,Alignment=2,OutlineColour=&H101010&,Outline=1,Shadow=1,BorderStyle=1",
        "instagram": "Fontsize=26,MarginV=40,Alignment=2,OutlineColour=&H202020&,Outline=2,Shadow=1,BorderStyle=1",
        "minimal": "Fontsize=24,MarginV=40,Alignment=2,OutlineColour=&H000000&,Outline=0,Shadow=0,BorderStyle=1",
        "colorful": "Fontsize=30,MarginV=40,Alignment=2,PrimaryColour=&H00FFFF&,OutlineColour=&H000000&,Outline=2,Shadow=1,BorderStyle=1"
    },
    "ffmpeg_path": "C:\\ffmpeg\\bin"
}

# Data models
@dataclass
class WhisperSegment:
    start: float
    end: float
    text: str


@dataclass
class MarketingChannel:
    name: str
    label: str
    kpis: List[Tuple[str, str]]


# Path utilities
def get_upload_folder() -> Path:
    """Return the upload folder path, ensuring it exists."""
    upload_folder = Path(os.getcwd()) / 'app4' / 'uploads'
    upload_folder.mkdir(parents=True, exist_ok=True)
    return upload_folder


# Timestamp conversion utilities
def seconds_to_srt_timestamp(seconds: float) -> str:
    """Convert float seconds to SRT timestamp (HH:MM:SS,mmm)."""
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    millis = int((td.total_seconds() - total_seconds) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


# Whisper segment processing
def split_whisper_segments(
    segments: List[Dict[str, Any]], 
    max_segment_duration: float = DEFAULT_MAX_SEGMENT_DURATION, 
    word_by_word: bool = False
) -> List[WhisperSegment]:
    """
    Split Whisper segments to create faster-paced subtitles.
    
    Args:
        segments: List of Whisper segments with start, end, and text fields
        max_segment_duration: Max sub-segment length in seconds
        word_by_word: If True, each word becomes its own sub-segment
    
    Returns:
        List of sub-segments with start, end, and text
    """
    new_segments = []
    
    for seg in segments:
        start_time = seg["start"]
        end_time = seg["end"]
        text = seg["text"].strip()
        
        if not text:
            continue
            
        duration = end_time - start_time
        words = text.split()
        
        if not words:
            continue
        
        # Word-by-word approach
        if word_by_word:
            word_duration = duration / len(words)
            current_start = start_time
            
            for word in words:
                current_end = current_start + word_duration
                new_segments.append(WhisperSegment(
                    start=current_start,
                    end=current_end,
                    text=word
                ))
                current_start = current_end
            continue
        
        # Chunk by max duration
        if duration <= max_segment_duration:
            new_segments.append(WhisperSegment(
                start=start_time,
                end=end_time,
                text=text
            ))
            continue
        
        # Calculate chunks
        chunk_count = math.ceil(duration / max_segment_duration)
        sub_duration = duration / chunk_count
        words_per_chunk = max(1, math.ceil(len(words) / chunk_count))
        
        idx = 0
        for c in range(chunk_count):
            sub_start = start_time + c * sub_duration
            sub_end = min(start_time + (c+1)*sub_duration, end_time)
            sub_words = words[idx: idx+words_per_chunk]
            idx += words_per_chunk
            
            if not sub_words:
                continue
                
            sub_text = " ".join(sub_words).strip()
            new_segments.append(WhisperSegment(
                start=sub_start,
                end=sub_end,
                text=sub_text
            ))
            
    return new_segments


def write_srt(segments: List[WhisperSegment], srt_path: Path, uppercase: bool = False) -> None:
    """Write segments to an SRT file."""
    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, start=1):
                start_ts = seconds_to_srt_timestamp(seg.start)
                end_ts = seconds_to_srt_timestamp(seg.end)
                text = seg.text.strip()
                
                if uppercase:
                    text = text.upper()
                
                f.write(f"{i}\n{start_ts} --> {end_ts}\n{text}\n\n")
        logger.info(f"Successfully wrote SRT file to {srt_path}")
    except Exception as e:
        logger.error(f"Error writing SRT file: {e}")
        raise


# FFmpeg utilities
def ensure_ffmpeg_available():
    """Make sure ffmpeg is available in the PATH."""
    os.environ['PATH'] += os.pathsep + VIDEO_PROCESSING_CONFIG["ffmpeg_path"]
    
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
        logger.info("FFmpeg is available in the PATH")
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.error("FFmpeg is not available in the PATH")
        raise RuntimeError("FFmpeg is not available. Please make sure it's installed properly.")


def extract_audio(video_path: Path, audio_path: Path) -> None:
    """Extract audio from video to MP3 format."""
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn",  # no video
            "-acodec", "mp3",
            str(audio_path)
        ], check=True, capture_output=True)
        logger.info(f"Successfully extracted audio to {audio_path}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Error extracting audio: {e.stderr.decode() if e.stderr else str(e)}")
        raise RuntimeError(f"Error extracting audio: {e}")


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path, 
                  font_family: str, caption_style: str, uppercase: bool = False) -> None:
    """Burn subtitles into video using FFmpeg."""
    # Get font and style config
    font_paths = VIDEO_PROCESSING_CONFIG["font_paths"]
    style_map = VIDEO_PROCESSING_CONFIG["style_map"]
    
    font_file = font_paths.get(font_family, font_paths["arial"])
    font_basename = os.path.basename(font_file)
    chosen_style = style_map.get(caption_style, style_map["tiktok"])
    
    force_style = f"Fontname={font_basename},{chosen_style}"
    srt_path_ffmpeg = str(srt_path).replace("\\", "/").replace("C:", "C\\:")
    subtitles_filter = f"subtitles='{srt_path_ffmpeg}:force_style={force_style}'"
    
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", subtitles_filter,
            "-codec:a", "copy",
            str(output_path)
        ], check=True, capture_output=True)
        logger.info(f"Successfully burned subtitles to {output_path}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Error burning subtitles: {e.stderr.decode() if e.stderr else str(e)}")
        raise RuntimeError(f"Error burning subtitles onto video: {e}")


# Analytics data processing
def get_marketing_channels() -> Dict[str, MarketingChannel]:
    """Return configuration for marketing channels and their KPIs."""
    return {
        "website": MarketingChannel(
            name="website",
            label="Website",
            kpis=[
                ("website_visits", "Antal Besøg"),
                ("website_unique", "Unikke Besøg"),
                ("website_session", "Session Varighed"),
                ("website_bounce", "Bounce Rate"),
                ("website_conversions", "Konverteringer")
            ]
        ),
        "social_media": MarketingChannel(
            name="social_media",
            label="Sociale Medier",
            kpis=[
                ("social_media_impressions", "Visninger"),
                ("social_media_new_followers", "Nye Følgere"),
                ("social_media_engagement", "Engagement"),
                ("social_media_clicks", "Klik"),
                ("social_media_conversions", "Konverteringer")
            ]
        ),
        "email": MarketingChannel(
            name="email",
            label="E-mail Marketing",
            kpis=[
                ("email_sent", "Udsendte E-mails"),
                ("email_open_rate", "Åbningsrate"),
                ("email_click_rate", "Klikrate"),
                ("email_conversions", "Konverteringer")
            ]
        ),
        "paid": MarketingChannel(
            name="paid",
            label="Betalt Søgeannoncering",
            kpis=[
                ("paid_impressions", "Visninger"),
                ("paid_clicks", "Klik"),
                ("paid_cpc", "CPC"),
                ("paid_conversions", "Konverteringer")
            ]
        )
    }


def extract_channel_data(channel: MarketingChannel) -> str:
    """Extract data from form for a specific marketing channel."""
    active = request.form.get(f"{channel.name}_active")
    if active == "on":
        lines = []
        for field, label in channel.kpis:
            value = request.form.get(field, "").strip()
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines) if lines else "Ingen data indsendt"
    return "Ingen data indsendt"


def create_analytics_prompt(period: str, channel_data: Dict[str, str]) -> str:
    """Create prompt for the analytics AI to generate insights."""
    raw_data = (
        f"Periode: {period}\n\n" +
        "\n\n".join([f"{channel_data[key]['label']}:\n{channel_data[key]['data']}" 
                     for key in channel_data])
    )
    
    return (
        "Du er en ekspert inden for forretningsanalyse og digital marketing. "
        "Analyser de følgende digitale kanaldata og giv konkrete, handlingsorienterede anbefalinger opdelt i flere kategorier. "
        "For hver kategori skal du levere et 'emne' (kort og slagkraftigt), et kort 'resumé' og yderligere 'detaljer' der forklarer, hvordan tiltagene kan øge ROI, reducere bounce rate og forbedre brugerengagement. "
        "Giv mindst 5 forskellige emner, og for hver indsats skal du inkludere en konkret handlingsplan samt en forventet procentvis forbedring (f.eks. 'Op til 25% forbedring'), hvis alle anbefalinger implementeres fuldt ud. "
        "Hvert indsigtsobjekt skal have felterne: 'emne', 'resumé', 'detaljer' og 'forbedring' (et tal). "
        "Skriv også et DiagramData JSON-objekt med to nøgler: 'etiketter' (liste med metriknavne) og 'værdier' (liste med numeriske værdier). "
        "JSON-objektet skal være gyldigt, uden ekstra tekst eller markdown.\n\n"
        "Svar venligst i præcis følgende format:\n\n"
        "Indsigt:\n"
        "[\n"
        "  {\n"
        "    \"emne\": \"<kategori navn>\",\n"
        "    \"resumé\": \"<kort oversigt over anbefalingerne for denne kategori>\",\n"
        "    \"detaljer\": \"<udvidet forklaring inkl. handlingsplan og forventet forbedring>\",\n"
        "    \"forbedring\": <tal>\n"
        "  },\n"
        "  ... (flere objekter)\n"
        "]\n\n"
        "DiagramData:\n"
        "<gyldigt JSON-objekt med to nøgler: 'etiketter' (liste med metriknavne) og 'værdier' (liste med numeriske værdier)>\n\n"
        f"Data:\n{raw_data}"
    )


def parse_analytics_response(response_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse the response from the analytics AI."""
    default_chart_data = {
        "etiketter": ["Metric 1", "Metric 2", "Metric 3"], 
        "værdier": [10, 20, 30]
    }
    
    # If no response or invalid format
    if not response_text or "DiagramData:" not in response_text:
        return [{
            "emne": "Ugyldigt Format",
            "resumé": response_text or "Tom respons",
            "detaljer": "",
            "forbedring": 0
        }], default_chart_data
    
    # Split response into insights and chart data
    parts = response_text.split("DiagramData:")
    insights_part = parts[0].strip()
    diagram_part = parts[1].strip()
    
    if insights_part.startswith("Indsigt:"):
        insights_json_str = insights_part[len("Indsigt:"):].strip()
    else:
        insights_json_str = insights_part
    
    # Parse insights
    try:
        insights = json.loads(insights_json_str)
    except Exception as e:
        logger.error(f"Error parsing insights JSON: {e}")
        insights = [{
            "emne": "Parsing Fejl",
            "resumé": f"Fejl ved parsing af indsigt JSON: {e}",
            "detaljer": "",
            "forbedring": 0
        }]
    
    # Parse chart data
    try:
        chart_data = json.loads(diagram_part)
        # Validate chart data structure
        if not isinstance(chart_data, dict) or "etiketter" not in chart_data or "værdier" not in chart_data:
            raise ValueError("Invalid chart data structure")
    except Exception as e:
        logger.error(f"Error parsing chart data JSON: {e}")
        chart_data = default_chart_data
    
    return insights, chart_data


# Route handlers
@app4_bp.route('/')
def index():
    """Main index page."""
    return render_template('index4.html')


@app4_bp.route('/uploaded/<filename>')
def uploaded_file(filename):
    """Serve uploaded files."""
    upload_folder = get_upload_folder()
    return send_from_directory(upload_folder, filename)


@app4_bp.route('/auto_caption', methods=['POST'])
def auto_caption():
    """Auto-caption route: transcribes video and adds captions."""
    try:
        # Validate input
        if 'video' not in request.files:
            flash('No video file uploaded', 'error')
            return redirect(url_for('app4.index'))
        
        video = request.files['video']
        if not video.filename:
            flash('No selected video file', 'error')
            return redirect(url_for('app4.index'))
        
        # Get parameters from form
        caption_style = request.form.get("caption_style", "tiktok")
        font_family = request.form.get("font_family", "arial")
        uppercase = (request.form.get("uppercase") == "on")
        word_by_word = (request.form.get("word_by_word") == "on")  # Added option for word-by-word
        
        # Setup paths
        upload_folder = get_upload_folder()
        video_path = upload_folder / video.filename
        audio_path = upload_folder / f"{os.path.splitext(video.filename)[0]}.mp3"
        srt_path = upload_folder / f"{os.path.splitext(video.filename)[0]}.srt"
        captioned_video_path = upload_folder / f"captioned_{video.filename}"
        
        # Save uploaded video
        video.save(video_path)
        logger.info(f"Saved uploaded video to {video_path}")
        
        # Make sure ffmpeg is available
        ensure_ffmpeg_available()
        
        # Extract audio
        extract_audio(video_path, audio_path)
        
        # Transcribe with Whisper
        try:
            logger.info(f"Loading Whisper {WHISPER_MODEL_SIZE} model")
            model = whisper.load_model(WHISPER_MODEL_SIZE)
            
            logger.info(f"Transcribing audio: {audio_path}")
            result = model.transcribe(str(audio_path), fp16=False)
            
            segments = result.get("segments", [])
            full_text = result.get("text", "").strip()
            
            if not segments or not full_text:
                raise ValueError("Transcription produced no segments or text")
                
            logger.info(f"Successfully transcribed {len(segments)} segments")
        except Exception as e:
            logger.error(f"Error during transcription: {e}")
            raise RuntimeError(f"Error during transcription: {e}")
        
        # Split segments for fast pacing
        segments_split = split_whisper_segments(
            segments, 
            max_segment_duration=DEFAULT_MAX_SEGMENT_DURATION, 
            word_by_word=word_by_word
        )
        logger.info(f"Split into {len(segments_split)} sub-segments")
        
        # Write SRT file
        write_srt(segments_split, srt_path, uppercase=uppercase)
        
        # Burn subtitles to video
        burn_subtitles(
            video_path, 
            srt_path, 
            captioned_video_path, 
            font_family, 
            caption_style, 
            uppercase
        )
        
        # Optional cleanup of temp files
        # audio_path.unlink(missing_ok=True)
        # Keep SRT for potential download
        
        return render_template(
            'auto_caption_result.html', 
            video_filename=captioned_video_path.name, 
            captions=full_text,
            srt_filename=srt_path.name
        )
        
    except Exception as e:
        logger.error(f"Error in auto_caption: {e}")
        flash(f"Error processing video: {e}", "error")
        return redirect(url_for('app4.index'))


@app4_bp.route('/improve_video', methods=['POST'])
def improve_video():
    """Improve video quality route."""
    try:
        if 'video' not in request.files:
            flash('No video file uploaded', 'error')
            return redirect(url_for('app4.index'))
            
        video = request.files['video']
        if not video.filename:
            flash('No selected video file', 'error')
            return redirect(url_for('app4.index'))
        
        # Get parameters (could add more options)
        enhance_resolution = request.form.get('enhance_resolution') == 'on'
        improve_audio = request.form.get('improve_audio') == 'on'
        stabilize = request.form.get('stabilize') == 'on'
        
        # Setup paths
        upload_folder = get_upload_folder()
        video_path = upload_folder / video.filename
        improved_video_path = upload_folder / f"improved_{video.filename}"
        
        # Save uploaded video
        video.save(video_path)
        logger.info(f"Saved uploaded video to {video_path}")
        
        # Ensure ffmpeg is available
        ensure_ffmpeg_available()
        
        # Build ffmpeg filters based on selected improvements
        filters = []
        
        if enhance_resolution:
            # Super-resolution using scale2x
            filters.append("scale2x")
            
        if improve_audio:
            # Enhance audio with compressor, normalize
            audio_filters = "compand=attacks=0:points=-80/-105|-62/-80|-15.4/-15.4|0/-12|20/-7.6,loudnorm"
        else:
            audio_filters = None
            
        if stabilize:
            # Video stabilization 
            filters.append("deshake")
        
        # Add basic enhancement filters if nothing specific was selected
        if not filters:
            filters.append("unsharp=5:5:1:5:5:1")  # Subtle sharpening
        
        video_filter_str = ",".join(filters)
        
        # Build ffmpeg command
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        
        if video_filter_str:
            ffmpeg_cmd.extend(["-vf", video_filter_str])
        
        if audio_filters:
            ffmpeg_cmd.extend(["-af", audio_filters])
        
        # Set output quality/codecs
        ffmpeg_cmd.extend([
            "-c:v", "libx264", 
            "-preset", "slow", 
            "-crf", "18",  # High quality
            str(improved_video_path)
        ])
        
        # Execute ffmpeg
        try:
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
            logger.info(f"Successfully improved video to {improved_video_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error enhancing video: {e.stderr.decode() if e.stderr else str(e)}")
            raise RuntimeError(f"Error enhancing video: {e}")
        
        # Generate improvement details
        improvements = []
        if enhance_resolution:
            improvements.append("Opløsning forbedret")
        if improve_audio:
            improvements.append("Lyd optimeret")
        if stabilize:
            improvements.append("Videostabilisering tilføjet")
        if not improvements:
            improvements.append("Generel billedkvalitet forbedret")
            
        improvement_details = "Videoen er blevet optimeret med: " + ", ".join(improvements)
        
        return render_template(
            'improve_video_result.html', 
            video_filename=improved_video_path.name, 
            improvement_details=improvement_details
        )
        
    except Exception as e:
        logger.error(f"Error improving video: {e}")
        flash(f"Error improving video: {e}", "error")
        return redirect(url_for('app4.index'))


@app4_bp.route('/product_ad', methods=['POST'])
def product_ad():
    """Generate product advertisement."""
    try:
        # Get form data
        title = request.form.get('title', '')
        description = request.form.get('description', '')
        image_url = request.form.get('image_url', '')
        
        if not title or not description:
            flash('Title and description are required', 'error')
            return redirect(url_for('app4.index'))
        
        # This could be enhanced with AI-generated content or templates
        ad_details = {
            "title": title,
            "description": description,
            "image_url": image_url
        }
        
        return render_template('product_ad_result.html', ad_details=ad_details)
        
    except Exception as e:
        logger.error(f"Error creating product ad: {e}")
        flash(f"Error creating product ad: {e}", "error")
        return redirect(url_for('app4.index'))


@app4_bp.route('/data')
def data_input():
    """Data input page."""
    period = request.args.get("period")
    if not period:
        flash('Period is required', 'error')
        return redirect(url_for('app4.index'))
        
    return render_template('index4.html', period=period, insights=[], chart_data=None)


@app4_bp.route('/analyze', methods=['POST'])
def analyze():
    """Analyze marketing data and provide insights."""
    try:
        period = request.form.get("period", "")
        if not period:
            flash('Period is required', 'error')
            return redirect(url_for('app4.index'))
        
        # Get marketing channels configuration
        channels = get_marketing_channels()
        
        # Extract data for each channel
        channel_data = {}
        for channel_key, channel in channels.items():
            data = extract_channel_data(channel)
            channel_data[channel_key] = {
                "label": channel.label,
                "data": data
            }
        
        # Create prompt for analysis
        prompt = create_analytics_prompt(period, channel_data)
        
        # Make API request (implement actual API call)
        try:
            # Placeholder for API call - replace with actual implementation
            # response = openai.chat.completions.create(...)
            # full_response = response.choices[0].message.content.strip()
            
            # Dummy response for now (replace with actual API call)
            full_response = """
            Indsigt:
            [
              {
                "emne": "Website Bounce Rate Optimering",
                "resumé": "Reducer bounce rate ved at forbedre landingsside-oplevelsen",
                "detaljer": "Implementer A/B-test af landingssider med fokus på mere engagerende indhold og klarere CTA-knapper. Dette vil forventeligt reducere bounce rate med op til 30% og øge konverteringsraten.",
                "forbedring": 30
              },
              {
                "emne": "Social Media Engagement Strategi",
                "resumé": "Øg engagement ved at optimere indholdskalender og -typer",
                "detaljer": "Skift fokus til video og interaktivt indhold som polls og quizzes, med optimal postering i peak-tider bestemt af platformanalyse. Implementering kan øge engagement med op til 40%.",
                "forbedring": 40
              }
            ]
            
            DiagramData:
            {"etiketter": ["Bounce Rate", "Engagement", "Konverteringer"], "værdier": [25, 40, 15]}
            """
            
            # Parse the response
            insights, chart_data = parse_analytics_response(full_response)
            
        except Exception as e:
            logger.error(f"Error generating analytics: {e}")
            insights = [{
                "emne": "Fejl",
                "resumé": f"Fejl ved generering af anbefalinger: {e}",
                "detaljer": "",
                "forbedring": 0
            }]
            chart_data = {"etiketter": ["Metric 1", "Metric 2", "Metric 3"], "værdier": [10, 20, 30]}
        
        return render_template('index4.html', period=period, insights=insights, chart_data=chart_data)
        
    except Exception as e:
        logger.error(f"Error in analyze: {e}")
        flash(f"Error analyzing data: {e}", "error")
        return redirect(url_for('app4.index'))


# Health check endpoint
@app4_bp.route('/health')
def health_check():
    """Simple health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}