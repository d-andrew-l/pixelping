# Contributing

Thanks for helping. A few ground rules keep PixelPing easy to run anywhere:

- **Standard library only.** No third-party Python packages, no build step for
  the UI. `index.html` is plain HTML, CSS and JavaScript.
- **Read-only on monitored hosts.** Agents may read and run inspection
  commands, never change anything.
- **Keep the agent small and portable.** It is streamed over ssh to every host
  and must run on a stock Linux `python3` (3.9+) without root.
- **Style:** match the surrounding code. Short comments that say why, not what.
  No em dashes in code, comments, docs or UI text.

## Development

```bash
cp config.example.json config.json
python3 pixelping.py
python3 -m unittest discover -s tests -v
```

Tests run on Linux (the agent test reads `/proc`). Add a test with any change
to parsing or validation, and note user-visible changes in `CHANGELOG.md`.

## Pull requests

Keep them focused, describe what you tested (which distro, which hosts), and
include a screenshot for UI changes. Report security issues privately to the
maintainer instead of opening a public issue.
