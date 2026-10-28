"""Optional registrations for the CLI owner; no second auth/client path."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .dictation import transcribe_audio


def register_media_commands(register: Callable, make_client: Callable) -> None:
    """register(name, handler, help, session=False) matches cli.build_parser's cmd.

    make_client(args) is the CLI owner's context-managed client factory, so
    credential overrides and HTTP policy remain owned by the existing CLI.
    """
    def transcribe(args):
        with make_client(args) as rc:
            text = transcribe_audio(rc, Path(args.path).read_bytes(), language=args.language)
        print(text)
        return 0

    def session_file(args):
        with make_client(args) as rc:
            result = rc.get_session_file(args.session_id, args.path)
        print(json.dumps(result))
        return 0

    def sync_file(args):
        with make_client(args) as rc:
            result = rc.put_synced_file(args.session_id, args.path, Path(args.local_file).read_bytes(),
                                        if_match_sha256=args.if_match)
        print(json.dumps(result))
        return 0

    def mount_file(args):
        with make_client(args) as rc:
            result = rc.mount_file(args.session_id, args.file_id, args.mount_path)
        print(json.dumps(result))
        return 0

    def prefetch(args):
        refs = json.loads(Path(args.references).read_text())
        if not isinstance(refs, list):
            raise ValueError("attachment references must be a JSON array")
        with make_client(args) as rc:
            result = rc.prefetch_attachments(args.session_id, refs, only_these=args.only_these)
        print(json.dumps(result))
        return 0

    parser = register("transcribe", transcribe, "transcribe a raw PCM16 16-kHz mono fixture (no microphone)")
    parser.add_argument("path")
    parser.add_argument("--language", default="en")
    parser = register("session-file", session_file, "read controller workspace file JSON", session=True)
    parser.add_argument("path")
    parser = register("sync-file", sync_file, "CAS-write /working/<file>", session=True)
    parser.add_argument("path")
    parser.add_argument("local_file")
    parser.add_argument("--if-match", required=True, help="content_sha256 returned by session-file")
    parser = register("mount-file", mount_file, "stage a Files API object", session=True)
    parser.add_argument("file_id")
    parser.add_argument("mount_path")
    parser = register("prefetch", prefetch, "enqueue attachment prefetch (ack is not completion)", session=True)
    parser.add_argument("references", help="JSON array of file_uuid/file_size objects")
    parser.add_argument("--only-these", action="store_true")
