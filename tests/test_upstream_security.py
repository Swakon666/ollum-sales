from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = spec_from_file_location(name, REPOSITORY_ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audio = _load_module(
    "whatsapp_audio_security",
    "upstream/whatsapp-mcp/whatsapp-mcp-server/audio.py",
)
scrape_do = _load_module(
    "scrape_do_security",
    "upstream/Scrapegraph-ai/scrapegraphai/docloaders/scrape_do.py",
)


def test_audio_paths_must_stay_inside_configured_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "voice.wav"
    inside.write_bytes(b"audio")
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"secret")
    monkeypatch.setenv("WHATSAPP_ALLOWED_MEDIA_ROOTS", str(allowed))

    assert audio._resolve_media_path(inside, must_exist=True) == inside.resolve()
    with pytest.raises(ValueError, match="outside"):
        audio._resolve_media_path(outside, must_exist=True)
    with pytest.raises(ValueError, match="outside"):
        audio._resolve_media_path(allowed / ".." / "secret.wav", must_exist=True)

    output_link = allowed / "converted.ogg"
    try:
        output_link.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="outside"):
            audio._resolve_media_path(output_link, must_exist=False)


def test_audio_conversion_uses_validated_absolute_arguments(tmp_path, monkeypatch):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "voice.ogg"
    monkeypatch.setenv("WHATSAPP_ALLOWED_MEDIA_ROOTS", str(tmp_path))
    monkeypatch.setattr(audio.shutil, "which", lambda _: "ffmpeg")
    run = Mock()
    monkeypatch.setattr(audio.subprocess, "run", run)

    result = audio.convert_to_opus_ogg(source, output, "32k", 24000)

    assert result == str(output.resolve())
    command = run.call_args.args[0]
    assert command[0] == "ffmpeg"
    assert str(source.resolve()) in command
    assert str(output.resolve()) in command
    assert run.call_args.kwargs["timeout"] == 120
    with pytest.raises(ValueError, match="Bitrate"):
        audio.convert_to_opus_ogg(source, output, "-i", 24000)
    with pytest.raises(ValueError, match="Bitrate"):
        audio.convert_to_opus_ogg(source, output, "9999M", 24000)


def test_public_http_url_rejects_private_and_resolved_internal_hosts(monkeypatch):
    with pytest.raises(ValueError, match="non-public"):
        scrape_do._public_http_url("http://127.0.0.1/private")
    with pytest.raises(ValueError, match="credentials"):
        scrape_do._public_http_url("https://user:pass@8.8.8.8/")

    monkeypatch.setattr(
        scrape_do.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                scrape_do.socket.AF_INET,
                scrape_do.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 443),
            )
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        scrape_do._public_http_url("https://internal.example/")

    assert scrape_do._public_http_url("https://8.8.8.8/path") == "https://8.8.8.8/path"


def test_scrape_do_api_keeps_target_in_query_params(monkeypatch):
    response = Mock(text="ok")
    request = Mock(return_value=response)
    monkeypatch.setattr(scrape_do.requests, "get", request)
    monkeypatch.delenv("API_SCRAPE_DO_URL", raising=False)

    result = scrape_do.scrape_do_fetch("token", "https://8.8.8.8/page")

    assert result == "ok"
    assert request.call_args.args == ("https://api.scrape.do",)
    assert request.call_args.kwargs["params"]["url"] == "https://8.8.8.8/page"
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["timeout"] == 60
