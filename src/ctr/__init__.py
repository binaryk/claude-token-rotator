"""ctr — claude-token-rotator.

Rotate long-lived Claude Code tokens on macOS: register them in the keychain,
watch their 5h / 7d utilisation, switch the active one before it runs out, and
roll parked `claude` sessions onto the new token.

Deliberately importless: `import ctr` must never pull in a module that shells
out. Import the submodule you need (`from ctr import shell`).
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
