#!/usr/bin/env python3
"""Send a test message to the local text-to-speech API."""

import json
import subprocess


def main() -> None:
    message = "Hello from the RedRat text-to-speech API."
    result = subprocess.run(
        [
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--request",
            "POST",
            "http://localhost:5000/api/voice/speak",
            "--header",
            "Content-Type: application/json",
            "--data",
            json.dumps({"text": message}),
        ],
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()