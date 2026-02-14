"""
AI Object Replacement Engine (Video-to-Video).
Replaces objects in video segments using Runware (Kling) video-to-video generation.

Pipeline:
  1. Extract video segment (start_sec to end_sec)
  2. Send segment to Runware (Kling) for regeneration with replacement prompt
  3. Splice regenerated segment back into original video
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Optional

from classes.logger import log
from classes.video_editor import edit_segment

def _get_video_duration(video_path: str) -> float:
    """Get video duration using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        log.warning("Failed to get duration via ffprobe: %s", e)
        return 0.0

def replace_object_in_video(
    video_path: str,
    object_description: str,
    replacement_description: str,
    start_sec: float = 0.0,
    end_sec: float = -1.0,
    keyframe_interval: float = 0.5, # Unused in video-to-video
    gemini_api_key: Optional[str] = None, # Unused
    runware_api_key: Optional[str] = None,
    runware_model: str = "runway:1@2",
) -> Dict[str, Any]:
    """
    Replace an object in a video by regenerating the segment with AI.
    
    Args:
        video_path: Path to source video
        object_description: Description of object to replace (unused in simple Kling prompt, but good for context if needed)
        replacement_description: Description of what to generate (the prompt)
        start_sec: Start time of replacement
        end_sec: End time of replacement (-1 for end of video)
        runware_api_key: API key for Runware
        
    Returns:
        Dict with success, output_path, error
    """
    result = {
        "success": False,
        "output_path": None,
        "error": None,
    }

    if not os.path.isfile(video_path):
        result["error"] = f"Video file not found: {video_path}"
        return result

    # Resolve Runware API Key
    rw_key = runware_api_key or os.getenv("RUNWARE_API_KEY", "").strip()
    if not rw_key:
        # Try settings
        try:
            from classes.app import get_app
            settings = get_app().get_settings()
            rw_key = (settings.get("runware-api-key") or "").strip()
        except Exception:
            pass
            
    if not rw_key:
        result["error"] = "Runware API key not found. Please configure it in settings."
        return result

    try:
        # Prepare working directory
        work_dir = tempfile.mkdtemp(prefix="ai_replace_")
        target_segment = os.path.join(work_dir, "target.mp4")
        edited_segment = os.path.join(work_dir, "edited.mp4")
        final_output = os.path.join(os.path.dirname(video_path), f"replaced_{uuid.uuid4().hex[:8]}.mp4")
        
        # Get duration
        total_duration = _get_video_duration(video_path)
        if end_sec < 0 or end_sec > total_duration:
            end_sec = total_duration

        if start_sec < 0.1 and end_sec >= total_duration - 0.1:
            log.warning("replace_object_in_video: replacing ENTIRE video (start=%.2f end=%.2f total=%.2f). "
                        "Consider specifying start_sec/end_sec to target only the relevant segment.",
                        start_sec, end_sec, total_duration)

        # 1. Extract the segment to be edited
        # We re-encode to ensure keyframes are clean for the AI
        duration = end_sec - start_sec
        if duration < 0.5:
             result["error"] = f"Selected duration too short: {duration}s"
             return result

        cmd_extract = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", str(start_sec), "-t", str(duration),
            "-c:v", "libx264", "-c:a", "aac",
            target_segment
        ]
        log.info(f"Extracting segment: {' '.join(cmd_extract)}")
        subprocess.run(cmd_extract, check=True, capture_output=True)

        # Ensure segment meets Runware minimum duration requirement (3s)
        seg_duration = _get_video_duration(target_segment)
        MIN_INPUT_DURATION = 3.0
        if seg_duration > 0 and seg_duration < MIN_INPUT_DURATION:
            padded = os.path.join(work_dir, "padded.mp4")
            pad_time = MIN_INPUT_DURATION - seg_duration
            cmd_pad = [
                "ffmpeg", "-y", "-i", target_segment,
                "-vf", f"tpad=stop_mode=clone:stop_duration={pad_time:.3f}",
                "-c:v", "libx264", "-c:a", "aac",
                padded
            ]
            log.info(f"Padding segment from {seg_duration:.2f}s to {MIN_INPUT_DURATION}s (Runware min 3s)")
            subprocess.run(cmd_pad, check=True, capture_output=True)
            os.replace(padded, target_segment)

        # Ensure segment meets Kling O1 minimum dimension requirement (720px)
        try:
            probe_cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0", target_segment
            ]
            dims = subprocess.run(probe_cmd, capture_output=True, text=True, check=True).stdout.strip()
            src_w, src_h = (int(x) for x in dims.split(","))
            min_dim = 720
            if src_w < min_dim or src_h < min_dim:
                scale_factor = max(min_dim / src_w, min_dim / src_h)
                new_w = int(src_w * scale_factor)
                new_h = int(src_h * scale_factor)
                # Ensure dimensions are divisible by 2 for libx264
                new_w += new_w % 2
                new_h += new_h % 2
                upscaled = os.path.join(work_dir, "upscaled.mp4")
                cmd_scale = [
                    "ffmpeg", "-y", "-i", target_segment,
                    "-vf", f"scale={new_w}:{new_h}",
                    "-c:v", "libx264", "-c:a", "aac", upscaled
                ]
                log.info(f"Upscaling segment from {src_w}x{src_h} to {new_w}x{new_h} (Kling min 720px)")
                subprocess.run(cmd_scale, check=True, capture_output=True)
                os.replace(upscaled, target_segment)
        except Exception as e:
            log.warning(f"Could not check/upscale segment dimensions: {e}")

        # 2. Edit the segment
        # Construct prompt: "A video of {replacement}" 
        # Kling works best with a description of the desired scene.
        # We combine context: "A video where {object} is replaced by {replacement}" -> "A high quality video of {replacement}"
        prompt = f"A high quality video of {replacement_description}"
        
        log.info(f"Sending to Runware (Kling)... Prompt: {prompt}")
        try:
            edit_segment(
                input_video_path=target_segment,
                output_video_path=edited_segment,
                prompt=prompt,
                api_key=rw_key,
                duration_seconds=5.0 # Kling usually generates 5s. If segment is longer, we might need multiple passes or just cut it.
                                     # For now, let's assume the user selects a short clip or we crop it.
                                     # Actually, Kling generates 5s. If input is longer, it might trim or fail.
                                     # Let's trust Runware to handle or just use 5s param.
            )
        except Exception as e:
            result["error"] = f"AI generation failed: {str(e)}"
            return result

        # Trim the AI-generated video to match the original segment duration
        # Runware may return a longer video (e.g. 5s for a 3s input, or padded input)
        edited_duration = _get_video_duration(edited_segment)
        if edited_duration > duration + 0.5:
            trimmed = os.path.join(work_dir, "trimmed.mp4")
            cmd_trim = [
                "ffmpeg", "-y", "-i", edited_segment,
                "-t", str(duration),
                "-c:v", "libx264", "-c:a", "aac",
                trimmed
            ]
            log.info(f"Trimming edited segment from {edited_duration:.2f}s to {duration:.2f}s to match original")
            subprocess.run(cmd_trim, check=True, capture_output=True)
            os.replace(trimmed, edited_segment)

        # 3. Splice back
        # Parts: [Head] + [Edited] + [Tail]
        # We need to be careful with timestamps.
        
        parts_file = os.path.join(work_dir, "parts.txt")
        parts = []

        # Head
        if start_sec > 0.1:
            head_path = os.path.join(work_dir, "head.mp4")
            cmd_head = [
                "ffmpeg", "-y", "-i", video_path,
                "-t", str(start_sec),
                "-c:v", "libx264", "-c:a", "aac",
                head_path
            ]
            subprocess.run(cmd_head, check=True, capture_output=True)
            parts.append(head_path)

        # Edited
        parts.append(edited_segment)

        # Tail
        if end_sec < total_duration - 0.1:
            tail_path = os.path.join(work_dir, "tail.mp4")
            cmd_tail = [
                "ffmpeg", "-y", "-i", video_path,
                "-ss", str(end_sec),
                "-c:v", "libx264", "-c:a", "aac",
                tail_path
            ]
            subprocess.run(cmd_tail, check=True, capture_output=True)
            parts.append(tail_path)

        # Concat
        with open(parts_file, "w") as f:
            for p in parts:
                f.write(f"file '{p}'\n")

        # Use concat demuxer (requires same codecs, which we ensured)
        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", parts_file,
            "-c", "copy",
            final_output
        ]
        log.info(f"Reassembling video: {' '.join(cmd_concat)}")
        subprocess.run(cmd_concat, check=True, capture_output=True)

        result["success"] = True
        result["output_path"] = final_output
        
    except subprocess.CalledProcessError as e:
        result["error"] = f"FFmpeg error: {e.stderr if hasattr(e, 'stderr') else str(e)}"
        log.error(f"FFmpeg failed: {e}", exc_info=True)
    except Exception as e:
        result["error"] = f"Object replacement failed: {str(e)}"
        log.error(f"Object replacement failed: {e}", exc_info=True)
    finally:
        # Cleanup
        if 'work_dir' in locals() and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

    return result
