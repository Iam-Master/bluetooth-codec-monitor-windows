# Contributing to Codec Monitor

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

1. Clone the repo and run `start.bat` to set up the Python virtual environment.
2. The app runs from `backend/app.py` (desktop mode) or `backend/monitor.py` (headless/browser mode).
3. Frontend files are in `frontend/` — plain HTML, CSS, and JavaScript (no build step needed).

## Making Changes

1. **Fork** the repository and create a feature branch from `main`.
2. Make your changes.
3. Test on Windows 10 or 11 with at least one Bluetooth audio device connected.
4. Submit a **pull request** with a clear description of what changed and why.

## Code Style

- **Python**: Follow PEP 8. Keep functions focused. Use type hints where practical.
- **JavaScript**: Vanilla JS, no frameworks. Keep the single-file structure of `app.js`.
- **CSS**: Use CSS custom properties (the `--var` tokens defined in `:root`). No utility-class frameworks.

## Reporting Issues

- Include your Windows version, Python version, and whether Alt A2DP Driver is installed.
- If it's a codec detection issue, include the codec name and device model.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
