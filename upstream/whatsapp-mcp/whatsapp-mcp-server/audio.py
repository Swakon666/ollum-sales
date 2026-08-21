import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_BITRATE_PATTERN = re.compile(r"^[1-9][0-9]{0,3}[kKmM]?$")


def _allowed_media_roots():
    configured = os.getenv("WHATSAPP_ALLOWED_MEDIA_ROOTS", "")
    raw_roots = (
        configured.split(os.pathsep)
        if configured
        else [tempfile.gettempdir(), "/data/whatsapp"]
    )
    return tuple(Path(root).expanduser().resolve() for root in raw_roots if root)


def _resolve_media_path(path, *, must_exist):
    candidate = Path(path).expanduser().resolve(strict=must_exist)
    if not any(candidate.is_relative_to(root) for root in _allowed_media_roots()):
        raise ValueError("Media path is outside WHATSAPP_ALLOWED_MEDIA_ROOTS")
    return candidate


def _validated_audio_options(bitrate, sample_rate):
    bitrate = str(bitrate)
    if not _BITRATE_PATTERN.fullmatch(bitrate):
        raise ValueError(
            "Bitrate must be a positive number with an optional k or M suffix"
        )
    suffix = bitrate[-1].lower() if bitrate[-1].isalpha() else ""
    numeric_value = int(bitrate[:-1] if suffix else bitrate)
    multiplier = 1_000 if suffix == "k" else 1_000_000 if suffix == "m" else 1
    bitrate_bps = numeric_value * multiplier
    if bitrate_bps < 6_000 or bitrate_bps > 1_000_000:
        raise ValueError("Bitrate must resolve to between 6000 and 1000000 bits/s")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise TypeError("Sample rate must be an integer")
    if sample_rate < 8000 or sample_rate > 192000:
        raise ValueError("Sample rate must be between 8000 and 192000 Hz")
    return bitrate, sample_rate


def convert_to_opus_ogg(input_file, output_file=None, bitrate="32k", sample_rate=24000):
    """
    Convert an audio file to Opus format in an Ogg container.

    Args:
        input_file (str): Path to the input audio file
        output_file (str, optional): Path to save the output file. If None, replaces the
                                    extension of input_file with .ogg
        bitrate (str, optional): Target bitrate for Opus encoding (default: "32k")
        sample_rate (int, optional): Sample rate for output (default: 24000)

    Returns:
        str: Path to the converted file

    Raises:
        FileNotFoundError: If the input file doesn't exist
        RuntimeError: If the ffmpeg conversion fails
    """
    input_path = _resolve_media_path(input_file, must_exist=True)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    bitrate, sample_rate = _validated_audio_options(bitrate, sample_rate)

    # If no output file is specified, replace the extension with .ogg
    if output_file is None:
        output_path = _resolve_media_path(
            input_path.with_suffix(".ogg"), must_exist=False
        )
    else:
        output_path = _resolve_media_path(output_file, must_exist=False)

    # Ensure the output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg is not installed or not available on PATH")

    # Build the ffmpeg command
    cmd = [
        ffmpeg,
        "-nostdin",
        "-i",
        str(input_path),
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        "-ar",
        str(sample_rate),
        "-application",
        "voip",  # Optimize for voice
        "-vbr",
        "on",  # Variable bitrate
        "-compression_level",
        "10",  # Maximum compression
        "-frame_duration",
        "60",  # 60ms frames (good for voice)
        "-y",  # Overwrite output file if it exists
        str(output_path),
    ]

    try:
        # Run the ffmpeg command and capture output
        subprocess.run(  # lgtm[py/command-line-injection]
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return str(output_path)
    except subprocess.CalledProcessError as e:
        details = (e.stderr or "")[-2000:]
        raise RuntimeError(f"Failed to convert audio with ffmpeg: {details}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg conversion timed out") from e


def convert_to_opus_ogg_temp(input_file, bitrate="32k", sample_rate=24000):
    """
    Convert an audio file to Opus format in an Ogg container and store in a temporary file.

    Args:
        input_file (str): Path to the input audio file
        bitrate (str, optional): Target bitrate for Opus encoding (default: "32k")
        sample_rate (int, optional): Sample rate for output (default: 24000)

    Returns:
        str: Path to the temporary file with the converted audio

    Raises:
        FileNotFoundError: If the input file doesn't exist
        RuntimeError: If the ffmpeg conversion fails
    """
    # Create a temporary file with .ogg extension
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        # Convert the audio
        convert_to_opus_ogg(input_file, temp_path, bitrate, sample_rate)
        return temp_path
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        # Clean up the temporary file if conversion fails
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


if __name__ == "__main__":
    # Example usage
    import sys

    if len(sys.argv) < 2:
        print("Usage: python audio.py input_file [output_file]")
        sys.exit(1)

    input_file = sys.argv[1]

    try:
        result = convert_to_opus_ogg_temp(input_file)
        print(f"Successfully converted to: {result}")
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)
