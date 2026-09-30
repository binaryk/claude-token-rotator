"""An in-memory stand-in for /usr/bin/security, for claude_login's `_run` seam.

Understands exactly the calls claude_login makes:
  find-generic-password -a A -s S -w
  add-generic-password -U -a A -s S -X HEX      (argv form)
  security -i  with that add line on stdin       (-i form, bounded like the real one)
  delete-generic-password -a A -s S
It records every argv so tests can assert where a secret did (not) travel.
"""

import shlex


class FakeSecurity(object):
    def __init__(self):
        self.items = {}  # (service, account) -> text
        self.calls = []  # argv lists
        self.stdin = []  # stdin payloads
        self.fail_writes = False

    def run(self, cmd, stdin_data=None):
        self.calls.append(list(cmd))
        self.stdin.append(stdin_data or "")
        args = list(cmd[1:])
        if args == ["-i"]:
            line = (stdin_data or "").strip()
            if len(stdin_data or "") > 4032:
                return 1, "", "line too long"
            args = shlex.split(line)
        verb = args[0]
        opts = self._opts(args[1:])
        key = (opts.get("-s"), opts.get("-a"))
        if verb == "find-generic-password":
            if key not in self.items:
                return 44, "", "The specified item could not be found in the keychain."
            return 0, self.items[key] + "\n", ""
        if verb == "add-generic-password":
            if self.fail_writes:
                return 1, "", "write refused"
            self.items[key] = bytes.fromhex(opts["-X"]).decode("utf-8")
            return 0, "", ""
        if verb == "delete-generic-password":
            return (0, "", "") if self.items.pop(key, None) is not None else (44, "", "")
        raise AssertionError("unexpected security call: %r" % (cmd,))

    @staticmethod
    def _opts(args):
        opts = {}
        index = 0
        while index < len(args):
            flag = args[index]
            if flag in ("-U", "-w"):
                opts[flag] = True
                index += 1
            else:
                opts[flag] = args[index + 1]
                index += 2
        return opts
