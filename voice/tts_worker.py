"""Offline text-to-speech worker process using pyttsx3."""

import os
import sys


def main() -> None:
    try:
        lines_mode = "--lines" in sys.argv

        if lines_mode:
            import pyttsx3

            engine = pyttsx3.init()
            rate_str = os.getenv("ORBIT_TTS_RATE", "180")
            try:
                rate = int(rate_str)
            except ValueError:
                rate = 180
            engine.setProperty("rate", rate)

            for line in sys.stdin:
                line_str = line.strip()
                if line_str:
                    engine.say(line_str)
                    engine.runAndWait()

            sys.exit(0)

        text = sys.stdin.read()
        if not text or not text.strip():
            sys.exit(0)

        import pyttsx3

        engine = pyttsx3.init()
        rate_str = os.getenv("ORBIT_TTS_RATE", "180")
        try:
            rate = int(rate_str)
        except ValueError:
            rate = 180

        engine.setProperty("rate", rate)
        engine.say(text)
        engine.runAndWait()
        sys.exit(0)
    except Exception as exc:
        sys.stderr.write(f"TTS worker error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
