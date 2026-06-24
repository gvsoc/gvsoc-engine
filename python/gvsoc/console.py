# SPDX-FileCopyrightText: 2026 ETH Zurich and University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

"""GVSoC Interactive Console — a REPL for controlling a running simulation."""

import bisect
import cmd
import os
import queue
import readline
import sys
import threading

import gvsoc.gvsoc_control as gvsoc_control


HISTORY_FILE = os.path.expanduser("~/.gvconsole_history")

TIME_UNITS = {
    'ps': 1,
    'ns': 1_000,
    'us': 1_000_000,
    'ms': 1_000_000_000,
}


def parse_duration(args):
    """Parse a duration string like '100 ns' or '100ns' into picoseconds.

    Returns (picoseconds, error_message). On error, picoseconds is None.
    """
    if not args:
        return None, "missing duration"

    parts = args.split()

    # Try "100ns" (no space)
    if len(parts) == 1:
        token = parts[0]
        for unit, factor in TIME_UNITS.items():
            if token.endswith(unit):
                try:
                    value = int(token[:-len(unit)])
                    return value * factor, None
                except ValueError:
                    return None, f"invalid number: {token[:-len(unit)]}"
        # No unit suffix — assume picoseconds
        try:
            return int(token), None
        except ValueError:
            return None, f"invalid duration: {token}"

    # Try "100 ns" (with space)
    if len(parts) == 2:
        try:
            value = int(parts[0])
        except ValueError:
            return None, f"invalid number: {parts[0]}"
        unit = parts[1].lower()
        if unit not in TIME_UNITS:
            return None, f"unknown unit '{unit}' (use ps, ns, us, ms)"
        return value * TIME_UNITS[unit], None

    return None, "expected: <number>[unit] or <number> <unit>"


def format_time(ps):
    """Format a time in picoseconds to human-readable form."""
    if ps is None:
        return "unknown"
    if ps >= 1_000_000_000:
        return f"{ps / 1_000_000_000:.3f} ms ({ps} ps)"
    elif ps >= 1_000_000:
        return f"{ps / 1_000_000:.3f} us ({ps} ps)"
    elif ps >= 1_000:
        return f"{ps / 1_000:.3f} ns ({ps} ps)"
    else:
        return f"{ps} ps"


def hexdump(data, base_addr=0):
    """Format binary data as a hex dump."""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = ' '.join(f'{b:02X}' for b in chunk[:8])
        if len(chunk) > 8:
            hex_part += '  ' + ' '.join(f'{b:02X}' for b in chunk[8:])
        # Pad hex part to fixed width
        hex_part = hex_part.ljust(49)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"0x{base_addr + offset:08X}: {hex_part} |{ascii_part}|")
    return '\n'.join(lines)


class GvsocConsole(cmd.Cmd):
    """Interactive console for controlling a running GVSoC simulation."""

    intro = "GVSoC Interactive Console. Type 'help' for available commands.\n"

    # Ordered command categories, mirroring the section dividers below. Each
    # entry is (key, title, description, [command names]). The key is used by
    # `help <category>` and is chosen to not collide with any command name.
    HELP_CATEGORIES = [
        ("control",   "Simulation control", "Run, step, stop and query the simulation",
            ["run", "step", "stepc", "stop", "status", "quit", "terminate", "exit"]),
        ("clock",     "Clock domains",      "List and select clock domains",
            ["clock"]),
        ("trace",     "Traces & events",    "Enable/disable traces and VCD events",
            ["trace", "event"]),
        ("memory",    "Memory access",      "Read and write memory via a router",
            ["mem"]),
        ("component", "Components",         "Send commands to platform components",
            ["component"]),
        ("debug",     "Breakpoints",        "Breakpoints, watchpoints and info",
            ["break", "watch", "rwatch", "delete", "masters", "info"]),
        ("registers", "CPU registers",      "Read CPU registers",
            ["reg"]),
        ("symbols",   "Symbol table",       "Load and resolve ELF symbols",
            ["symbol"]),
        ("settings",  "Settings",           "Configure router/iss/tracefile",
            ["set"]),
        ("script",    "Scripting",          "Run commands from a file",
            ["script"]),
    ]

    def __init__(self, host='localhost', port=42951):
        super().__init__()
        self.host = host
        self.port = port
        self.proxy = None
        self.router = None
        self.router_path = '**/chip/soc/axi_ico'
        self.component_cache = {}  # path -> pointer string
        self.notifications = queue.Queue()
        self.clock_domain = None  # Selected clock domain path
        self.clock_domains_cache = None  # Cached list of clock domains
        self.breakpoints = {}  # id -> {'addr': int}
        self.watchpoints = {}  # id -> {'addr': int, 'size': int, 'type': 'write'|'read'}
        self.next_bp_id = 1
        self.iss_path = '**/chip/soc/fc/core'
        self.iss_ptr = None  # cached ISS component pointer (current core, for reg/registers)
        self.cores = None  # cached list of all core pointers for breakpoints/watchpoints (lazy)
        self.symbols = {}  # name -> addr
        self.sym_addrs = []  # sorted list of (addr, name) for reverse lookup
        self._loaded_binaries = set()  # engine binaries already auto-loaded (see _sync_binaries)
        self._applied_binaries_seq = 0  # last reader.binaries_changed_seq we reacted to
        self.trace_file = None  # path to trace output file
        self.trace_file_pos = 0  # current read position
        self.gui_embedded = False  # True when launched behind the GUI relay (sync clock selection)
        self._applied_clock_seq = 0  # reader sequence of the last GUI clock selection we applied
        self._update_prompt()

    def preloop(self):
        """Connect to proxy and load history."""
        # Load readline history
        try:
            readline.read_history_file(HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(1000)

        # Connect to proxy
        try:
            self.proxy = gvsoc_control.Proxy(self.host, self.port)
            self.proxy.register_exit_callback(self._on_exit)
            print(f"Connected to GVSoC proxy at {self.host}:{self.port}")
        except (ConnectionRefusedError, OSError) as e:
            print(f"Error: cannot connect to GVSoC proxy at {self.host}:{self.port} ({e})")
            sys.exit(1)

        # Query the engine's registered binaries and load their symbols, so breakpoints by symbol
        # name work out of the box without a manual `symbol load`. The query is a synchronous
        # round-trip, so it is race-free even in script mode where the first command runs immediately.
        self._sync_binaries()

        self._update_prompt()

    def postloop(self):
        """Save history and clean up."""
        try:
            readline.write_history_file(HISTORY_FILE)
        except OSError:
            pass
        if self.proxy:
            try:
                self.proxy.close()
            except Exception:
                pass

    def precmd(self, line):
        """Drain notification queue and update prompt before each command."""
        self._drain_notifications()
        self._sync_binaries_if_changed()
        self._sync_clock_from_gui()
        self._update_prompt()
        return line

    def _notify_clock_selection(self, path):
        """Mirror a console-originated clock selection to the GUI timeline icon.

        Only when embedded in the GUI: the relay intercepts this line and never forwards it to the
        engine, so it is fire-and-forget (no reply to wait for). Selections coming FROM the GUI are
        applied via _sync_clock_from_gui and never reach here, so there is no feedback loop.
        """
        if not self.gui_embedded or not path or self.proxy is None:
            return
        try:
            self.proxy._send_cmd('clock_select %s' % path, wait_reply=False)
        except Exception:
            pass

    def _sync_clock_from_gui(self):
        """Adopt a clock-domain selection pushed from the GUI (icon / signal right-click).

        The reader stamps each inbound notification with an incrementing sequence so a re-selection of
        the same domain is still seen. Applied locally only (used by 'stepc'/'clock info'); not echoed.
        """
        reader = self.proxy.reader if self.proxy else None
        if reader is None:
            return
        seq = getattr(reader, 'active_clock_seq', 0)
        if seq != self._applied_clock_seq:
            self._applied_clock_seq = seq
            self.clock_domain = getattr(reader, 'active_clock', None)

    def postcmd(self, stop, line):
        """Update prompt after each command."""
        self._drain_notifications()
        self._sync_binaries_if_changed()
        self._update_prompt()
        return stop

    def emptyline(self):
        """Do nothing on empty input (don't repeat last command)."""
        pass

    def do_EOF(self, arg):
        """Exit the console (Ctrl-D)."""
        print()
        return True

    def do_exit(self, arg):
        """Exit the console."""
        return True

    def _cmd_summary(self, name):
        """Return the first non-blank line of a command's docstring."""
        doc = getattr(self, 'do_' + name, None).__doc__ or ''
        for line in doc.strip().splitlines():
            line = line.strip()
            if line:
                return line
        return ''

    def _print_group(self, title, names, width):
        """Print a category header followed by its commands and summaries.

        `width` is the command-name column width, passed in so descriptions
        align across every group, not just within one.
        """
        print(f"{title}:")
        for name in names:
            print(f"  {name.ljust(width)}  {self._cmd_summary(name)}")

    def do_help(self, arg):
        """Show grouped help, a single category, or one command's details.

        Usage: help [command|category]
          help            - list all commands grouped by category
          help <category> - list the commands in one category
          help <command>  - show full details for a command

        Categories: control, clock, trace, memory, component, debug,
                    registers, symbols, settings, script
        """
        arg = arg.strip()

        # A specific command always wins over a category of the same spelling.
        if arg and hasattr(self, 'do_' + arg):
            return super().do_help(arg)

        # Width of the command-name column, shared by every group so the
        # description column lines up across the whole listing.
        all_cmds = [n[3:] for n in dir(self)
                    if n.startswith('do_') and n not in ('do_EOF', 'do_help')]
        width = max((len(n) for n in all_cmds), default=0)

        if arg:
            for key, title, _desc, names in self.HELP_CATEGORIES:
                if arg.lower() in (key, title.lower()):
                    self._print_group(title, names, width)
                    return
            print(f"No help available for '{arg}'.")
            return

        # No argument: grouped overview of everything.
        categorized = set()
        for key, title, _desc, names in self.HELP_CATEGORIES:
            self._print_group(title, names, width)
            categorized.update(names)
            print()

        # Any command not listed in a category lands in "Other", so a newly
        # added command is never silently hidden from help.
        others = sorted(c for c in all_cmds if c not in categorized)
        if others:
            self._print_group("Other", others, width)
            print()

        print("type 'help <command>' for details, 'help <category>' for a group")

    def complete_help(self, text, line, begidx, endidx):
        """Complete command names and category keys after `help`."""
        names = {n[3:] for n in dir(self) if n.startswith('do_') and n != 'do_EOF'}
        names |= {key for key, _t, _d, _c in self.HELP_CATEGORIES}
        return sorted(n for n in names if n.startswith(text))

    # ──────────────────────────────────────────────
    # Simulation Control
    # ──────────────────────────────────────────────

    def do_run(self, arg):
        """Run the simulation.

        Usage: run [duration] [unit]
          run           - run until stopped or simulation ends
          run 1000      - run for 1000 ps
          run 1 us      - run for 1 microsecond
          run 100ns     - run for 100 nanoseconds

        Units: ps, ns, us, ms (default: ps)
        """
        self._resume_if_halted()
        arg = arg.strip()
        if not arg:
            if self.breakpoints or self.watchpoints:
                # Free-run at full engine speed; the engine stops itself and notifies us when a
                # breakpoint/watchpoint is hit (see ISS stop_for_debug), so there is no need to step
                # in small chunks and poll — which used to make a breakpointed run ~100x slower.
                print("Running until breakpoint or end...")
                self.proxy.run()
                hit = self.proxy.reader.wait_debug_stop()
                if self.proxy.reader.sim_has_exited:
                    print("Simulation exited.")
                elif hit:
                    self._check_breakpoint_hit()
            else:
                self.proxy.run()
                print("Simulation running.")
        else:
            ps, err = parse_duration(arg)
            if err:
                print(f"Error: {err}")
                return
            self._step(ps)

    def do_step(self, arg):
        """Step the simulation by a duration (default: 1 ns).

        Usage: step [duration] [unit]
          step          - step 1 ns
          step 100      - step 100 ps
          step 10 us    - step 10 microseconds

        Units: ps, ns, us, ms (default: ps)
        """
        self._resume_if_halted()
        if not arg:
            ps = 1000  # 1 ns default
        else:
            ps, err = parse_duration(arg)
            if err:
                print(f"Error: {err}")
                return
        self._step(ps)

    def do_stepc(self, arg):
        """Step the simulation by clock cycles (requires a selected clock domain).

        Usage: stepc [count]
          stepc         - step 1 cycle
          stepc 10      - step 10 cycles
          stepc 1000    - step 1000 cycles

        Use 'clock select <path>' to select a clock domain first.
        Stepping is cycle-accurate even if the clock frequency changes mid-step.
        """
        self._resume_if_halted()
        if self.clock_domain is None:
            print("Error: no clock domain selected. Use 'clock list' and 'clock select <path>'.")
            return

        count = 1
        if arg:
            try:
                count = int(arg)
            except ValueError:
                print(f"Error: invalid cycle count '{arg}'")
                return

        # Find clock index
        domains = self.proxy.get_clock_domains()
        clock_idx = None
        for i, d in enumerate(domains):
            if d['path'] == self.clock_domain:
                clock_idx = i
                break
        if clock_idx is None:
            print(f"Error: clock domain '{self.clock_domain}' not found")
            return

        # Use true cycle-based stepping. Clear the interrupted-step reason first; the reader sets it
        # only if the engine reports the step was stopped before completing (e.g. toolbar stop, or
        # later a breakpoint).
        self.proxy.reader.step_stop_reason = None
        reply = self.proxy.step_cycles(clock_idx, count)
        try:
            timestamp = int(reply.strip())
            self.proxy.reader.timestamp = timestamp
        except ValueError:
            timestamp = None
        self._drain_notifications()
        self._display_new_traces()
        self._update_prompt()
        time_str = format_time(timestamp) if timestamp is not None else "unknown"
        stop_reason = self.proxy.reader.step_stop_reason
        if stop_reason:
            print(f"Step interrupted ({stop_reason}) before completing {count} cycle(s). "
                f"Time: {time_str}")
        else:
            print(f"Stepped {count} cycle(s). Time: {time_str}")
        self._check_breakpoint_hit()

    def do_stop(self, arg):
        """Stop the simulation."""
        self.proxy.stop()
        print("Simulation stopped.")

    def do_quit(self, arg):
        """Quit the simulation.

        Usage: quit [status]
          quit      - quit with status 0
          quit 1    - quit with status 1
        """
        status = 0
        if arg:
            try:
                status = int(arg)
            except ValueError:
                print(f"Error: invalid status '{arg}'")
                return
        self.proxy.quit(status)
        print(f"Quit requested (status={status}).")
        return True

    def do_terminate(self, arg):
        """Force-terminate the simulation."""
        self.proxy.terminate()
        print("Terminate sent.")
        return True

    def do_status(self, arg):
        """Show simulation status (running/stopped, current time)."""
        reader = self.proxy.reader
        if reader.sim_has_exited:
            state = f"exited (code={reader.sim_exit_code})"
        elif reader.running:
            state = "running"
        else:
            state = "stopped"
        print(f"State: {state}")
        print(f"Time:  {format_time(reader.timestamp)}")
        if self.clock_domain:
            info = self._get_clock_info(self.clock_domain)
            if info:
                print(f"Clock: {self.clock_domain} @ {info['frequency']} Hz, "
                      f"cycle={info['cycles']}, period={info['period']} ps")

    # ──────────────────────────────────────────────
    # Clock Domain Control
    # ──────────────────────────────────────────────

    def do_clock(self, arg):
        """Manage clock domains.

        Usage:
          clock list              - list all clock domains with their frequency/period
          clock select <path>     - select a clock domain for cycle-based stepping
          clock info              - show info about the selected clock domain

        After selecting a clock domain, use 'stepc [N]' to step by cycles.
        """
        parts = arg.split(None, 1)
        if not parts:
            print("Usage: clock list|select|info")
            return

        subcmd = parts[0]

        if subcmd == 'list':
            domains = self.proxy.get_clock_domains()
            if not domains:
                print("No clock domains found.")
                return
            print(f"{'Path':<50} {'Freq (Hz)':<15} {'Period (ps)':<15} {'Cycles'}")
            print("-" * 95)
            for d in domains:
                selected = " *" if d['path'] == self.clock_domain else ""
                print(f"{d['path']:<50} {d.get('frequency', 0):<15} "
                      f"{d.get('period', 0):<15} {d.get('cycles', 0)}{selected}")
            self.clock_domains_cache = domains

        elif subcmd == 'select':
            if len(parts) < 2:
                print("Usage: clock select <path>")
                return
            path = parts[1].strip()
            # Verify the domain exists
            domains = self.proxy.get_clock_domains()
            self.clock_domains_cache = domains
            found = None
            for d in domains:
                if d['path'] == path:
                    found = d
                    break
                # Also match partial paths (e.g., user types "soc_clock" and it matches "/.../soc_clock")
                if d['path'].endswith('/' + path) or d['path'].endswith('/' + path + '/'):
                    found = d
                    break
            if found is None:
                print(f"Error: clock domain '{path}' not found. Use 'clock list' to see available domains.")
                return
            self.clock_domain = found['path']
            self._notify_clock_selection(self.clock_domain)
            print(f"Selected clock domain: {self.clock_domain}")
            print(f"  Frequency: {found.get('frequency', 0)} Hz")
            print(f"  Period:    {found.get('period', 0)} ps")
            print(f"  Cycles:    {found.get('cycles', 0)}")

        elif subcmd == 'info':
            if self.clock_domain is None:
                print("No clock domain selected. Use 'clock select <path>'.")
                return
            info = self._get_clock_info(self.clock_domain)
            if info:
                print(f"Clock domain: {self.clock_domain}")
                print(f"  Frequency: {info.get('frequency', 0)} Hz")
                print(f"  Period:    {info.get('period', 0)} ps")
                print(f"  Cycles:    {info.get('cycles', 0)}")
        else:
            print(f"Unknown clock subcommand: {subcmd}")
            print("Usage: clock list|select|info")

    def complete_clock(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['list', 'select', 'info']
            return [s for s in subcmds if s.startswith(text)]
        if len(parts) >= 2 and parts[1] == 'select':
            # Complete with cached domain paths
            if self.clock_domains_cache:
                paths = [d['path'] for d in self.clock_domains_cache]
                return [p for p in paths if p.startswith(text)]
        return []

    def _get_clock_info(self, path):
        """Query current clock domain info via the component handle_command."""
        if path not in self.component_cache:
            # get_path() returns paths with leading '/', but get_component expects
            # paths without it
            lookup_path = path.lstrip('/')
            ptr = self.proxy._get_component(lookup_path)
            if not ptr or ptr.strip() == '' or ptr.strip() == '0x0':
                print(f"Error: cannot find component '{path}'")
                return None
            self.component_cache[path] = ptr.strip()
        ptr = self.component_cache[path]
        result = self.proxy._send_cmd(f'component {ptr} info')
        if not result or not result.strip():
            return None
        info = {}
        for kv in result.strip().split():
            if '=' in kv:
                k, v = kv.split('=', 1)
                try:
                    info[k] = int(v)
                except ValueError:
                    info[k] = v
        return info

    # ──────────────────────────────────────────────
    # Trace / Event Control
    # ──────────────────────────────────────────────

    def do_trace(self, arg):
        """Manage simulation traces.

        Usage:
          trace add <pattern>      - enable traces matching regex
          trace remove <pattern>   - disable traces matching regex
          trace level <level>      - set trace level (error/warning/info/debug/trace)
        """
        parts = arg.split(None, 1)
        if len(parts) < 1:
            print("Usage: trace add|remove|level <argument>")
            return

        subcmd = parts[0]
        subarg = parts[1] if len(parts) > 1 else ''

        if subcmd == 'add':
            if not subarg:
                print("Usage: trace add <pattern>")
                return
            self.proxy.trace_add(subarg)
            print(f"Trace enabled: {subarg}")
        elif subcmd == 'remove':
            if not subarg:
                print("Usage: trace remove <pattern>")
                return
            self.proxy.trace_remove(subarg)
            print(f"Trace disabled: {subarg}")
        elif subcmd == 'level':
            if not subarg:
                print("Usage: trace level <error|warning|info|debug|trace>")
                return
            self.proxy.trace_level(subarg)
            print(f"Trace level set to: {subarg}")
        else:
            print(f"Unknown trace subcommand: {subcmd}")
            print("Usage: trace add|remove|level <argument>")

    def complete_trace(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['add', 'remove', 'level']
            return [s for s in subcmds if s.startswith(text)]
        if len(parts) >= 2 and parts[1] == 'level':
            levels = ['error', 'warning', 'info', 'debug', 'trace']
            return [l for l in levels if l.startswith(text)]
        return []

    def do_event(self, arg):
        """Manage VCD event tracing.

        Usage:
          event add <pattern>      - enable events matching regex
          event remove <pattern>   - disable events matching regex
        """
        parts = arg.split(None, 1)
        if len(parts) < 1:
            print("Usage: event add|remove <pattern>")
            return

        subcmd = parts[0]
        subarg = parts[1] if len(parts) > 1 else ''

        if subcmd == 'add':
            if not subarg:
                print("Usage: event add <pattern>")
                return
            self.proxy.event_add(subarg)
            print(f"Event enabled: {subarg}")
        elif subcmd == 'remove':
            if not subarg:
                print("Usage: event remove <pattern>")
                return
            self.proxy.event_remove(subarg)
            print(f"Event disabled: {subarg}")
        else:
            print(f"Unknown event subcommand: {subcmd}")
            print("Usage: event add|remove <pattern>")

    def complete_event(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['add', 'remove']
            return [s for s in subcmds if s.startswith(text)]
        return []

    # ──────────────────────────────────────────────
    # Memory Access
    # ──────────────────────────────────────────────

    def _get_router(self, path=None):
        """Get or create a Router instance for memory access."""
        path = path or self.router_path
        if self.router is None or path != self.router_path:
            try:
                self.router = gvsoc_control.Router(self.proxy, path)
                self.router_path = path
            except Exception as e:
                print(f"Error: cannot access router at '{path}': {e}")
                return None
        return self.router

    def do_mem(self, arg):
        """Read/write memory through a router component.

        Usage:
          mem read <addr> <size>         - read bytes, show hex dump
          mem write <addr> <hex_bytes>   - write hex bytes (e.g. "01 02 FF")
          mem read32 <addr> [count]      - read 32-bit word(s)
          mem write32 <addr> <value>     - write 32-bit word

        Addresses can be hex (0x...) or decimal.
        Default router: **/chip/soc/axi_ico (change with 'set router <path>')
        """
        parts = arg.split()
        if len(parts) < 2:
            print("Usage: mem read|write|read32|write32 <addr> ...")
            return

        subcmd = parts[0]

        if subcmd == 'read':
            if len(parts) < 3:
                print("Usage: mem read <addr> <size>")
                return
            try:
                addr = int(parts[1], 0)
                size = int(parts[2], 0)
            except ValueError as e:
                print(f"Error: {e}")
                return
            router = self._get_router()
            if router is None:
                return
            try:
                data = router.mem_read(addr, size)
                print(hexdump(data, addr))
            except RuntimeError as e:
                print(f"Error: {e}")

        elif subcmd == 'write':
            if len(parts) < 3:
                print("Usage: mem write <addr> <hex_bytes>")
                return
            try:
                addr = int(parts[1], 0)
            except ValueError as e:
                print(f"Error: {e}")
                return
            hex_str = ''.join(parts[2:])
            try:
                data = bytes.fromhex(hex_str)
            except ValueError as e:
                print(f"Error: invalid hex data: {e}")
                return
            router = self._get_router()
            if router is None:
                return
            try:
                router.mem_write(addr, len(data), data)
                print(f"Wrote {len(data)} bytes at 0x{addr:08X}")
            except RuntimeError as e:
                print(f"Error: {e}")

        elif subcmd == 'read32':
            if len(parts) < 2:
                print("Usage: mem read32 <addr> [count]")
                return
            try:
                addr = int(parts[1], 0)
            except ValueError as e:
                print(f"Error: {e}")
                return
            count = 1
            if len(parts) >= 3:
                try:
                    count = int(parts[2], 0)
                except ValueError as e:
                    print(f"Error: {e}")
                    return
            router = self._get_router()
            if router is None:
                return
            try:
                for i in range(count):
                    val = router.mem_read_int(addr + i * 4, 4)
                    print(f"0x{addr + i * 4:08X}: 0x{val:08X} ({val})")
            except RuntimeError as e:
                print(f"Error: {e}")

        elif subcmd == 'write32':
            if len(parts) < 3:
                print("Usage: mem write32 <addr> <value>")
                return
            try:
                addr = int(parts[1], 0)
                value = int(parts[2], 0)
            except ValueError as e:
                print(f"Error: {e}")
                return
            router = self._get_router()
            if router is None:
                return
            try:
                router.mem_write_int(addr, 4, value)
                print(f"Wrote 0x{value:08X} at 0x{addr:08X}")
            except RuntimeError as e:
                print(f"Error: {e}")

        else:
            print(f"Unknown mem subcommand: {subcmd}")
            print("Usage: mem read|write|read32|write32 <addr> ...")

    def complete_mem(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['read', 'write', 'read32', 'write32']
            return [s for s in subcmds if s.startswith(text)]
        return []

    # ──────────────────────────────────────────────
    # Component Interaction
    # ──────────────────────────────────────────────

    def do_component(self, arg):
        """Send a command to a component.

        Usage:
          component <path> [command] [args...]

        The component path is resolved to an internal pointer (cached).
        If no command is given, just verifies the component exists.

        Examples:
          component **/chip/soc/axi_ico
          component **/chip/soc/uart0 setup baudrate=115200
        """
        parts = arg.split()
        if not parts:
            print("Usage: component <path> [command] [args...]")
            return

        path = parts[0]
        cmd_args = parts[1:]

        # Resolve path to pointer (with cache)
        if path not in self.component_cache:
            ptr = self.proxy._get_component(path)
            if not ptr or ptr.strip() == '':
                print(f"Error: component not found: {path}")
                return
            self.component_cache[path] = ptr.strip()

        ptr = self.component_cache[path]

        if not cmd_args:
            print(f"Component '{path}' found (handle: {ptr})")
            return

        # Send component command
        cmd_str = f"component {ptr} {' '.join(cmd_args)}"
        result = self.proxy._send_cmd(cmd_str)
        if result and result.strip():
            print(result.strip())
        else:
            print("OK")

    # ──────────────────────────────────────────────
    # Breakpoints
    # ──────────────────────────────────────────────

    def _get_iss(self):
        """Get or resolve the ISS component pointer."""
        if self.iss_ptr is None:
            ptr = self.proxy._get_component(self.iss_path)
            if not ptr or ptr.strip() == '' or ptr.strip() == '0x0':
                print(f"Error: ISS component not found at '{self.iss_path}'")
                print("Use 'set iss <path>' to configure the ISS component path.")
                return None
            self.iss_ptr = ptr.strip()
        return self.iss_ptr

    def _iss_cmd(self, cmd):
        """Send a command to the ISS component. Returns the response or None on error."""
        ptr = self._get_iss()
        if ptr is None:
            return None
        result = self.proxy._send_cmd(f'component {ptr} {cmd}')
        return result.strip() if result else None

    def _get_cores(self):
        """Return the component pointers of all cores (cached).

        Breakpoints and watchpoints are per-core (each core only checks its own), so they must be
        set on every core. Falls back to the single selected ISS if the engine cannot enumerate
        cores (older engine).
        """
        if self.cores is None:
            try:
                cores = self.proxy.get_cores()
            except Exception:
                cores = []
            if not cores:
                ptr = self._get_iss()
                cores = [ptr] if ptr else []
            self.cores = cores
        return self.cores

    def _cores_cmd(self, cmd):
        """Send a command to every core; return a list of (ptr, result)."""
        results = []
        for ptr in self._get_cores():
            result = self.proxy._send_cmd(f'component {ptr} {cmd}')
            results.append((ptr, result.strip() if result else None))
        return results

    def _find_hit(self):
        """Find what stopped the sim. Returns (ptr, kind, status) or None:
          ('break', a core ptr) for a breakpoint (per-core),
          ('watch', None)       for a watchpoint (central engine facility, any master).
        status is the raw 'hit=1 ...' string."""
        if self.breakpoints:
            for ptr in self._get_cores():
                result = self.proxy._send_cmd(f'component {ptr} breakpoint_status')
                if result and 'hit=1' in result:
                    return (ptr, 'break', result.strip())
        if self.watchpoints:
            result = self.proxy._send_cmd('watchpoint_status')
            if result and 'hit=1' in result:
                return (None, 'watch', result.strip())
        return None

    def _is_halted(self):
        """Check if any core is halted at a breakpoint or watchpoint."""
        return self._find_hit() is not None

    def _is_breakpoint_hit(self):
        """Check if a breakpoint or watchpoint was hit on any core."""
        return self._is_halted()

    def _check_breakpoint_hit(self):
        """Report the breakpoint/watchpoint that was hit, on whichever core hit it."""
        hit = self._find_hit()
        if hit is None:
            return
        ptr, kind, result = hit

        if kind == 'break':
            # Make the core that hit the current one, so `reg` shows its state, and note which core.
            self.iss_ptr = ptr
            cores = self._get_cores()
            core_note = f" (core {cores.index(ptr)})" if ptr in cores and len(cores) > 1 else ""
            addr = None
            for part in result.split():
                if part.startswith('addr='):
                    addr = int(part.split('=')[1], 0)
            if addr is not None:
                bp_id = next((bid for bid, bp in self.breakpoints.items() if bp['addr'] == addr), None)
                loc = f"0x{addr:08X}"
                sym = self._addr_to_symbol(addr)
                if sym:
                    loc += f" <{sym}>"
                label = f"Breakpoint {bp_id}" if bp_id is not None else "Breakpoint"
                print(f"{label} hit at {loc}{core_note}")
        else:
            # Central watchpoint: the status reports the master (any master, not only cores).
            addr = None
            is_write = True
            master = None
            for part in result.split():
                if part.startswith('addr='):
                    addr = int(part.split('=')[1], 0)
                elif part.startswith('is_write='):
                    is_write = (part.split('=')[1] != '0')
                elif part.startswith('master='):
                    master = part.split('=', 1)[1]
            if addr is not None:
                wp_id = next((wid for wid, wp in self.watchpoints.items()
                              if wp['addr'] <= addr < wp['addr'] + wp['size']), None)
                loc = f"0x{addr:08X}"
                sym = self._addr_to_symbol(addr)
                if sym:
                    loc += f" <{sym}>"
                wkind = "Write" if is_write else "Read"
                label = f"{wkind} watchpoint {wp_id}" if wp_id is not None else f"{wkind} watchpoint"
                master_note = f" by {master}" if master else ""
                print(f"{label} hit at {loc}{master_note}")

    def _resume_if_halted(self):
        """Clear a pending breakpoint (per-core) or watchpoint (central) hit so the next run makes
        progress. No-op on cores not halted."""
        if self.breakpoints:
            self._cores_cmd('resume')
        if self.watchpoints:
            self.proxy._send_cmd('watchpoint_resume')

    def do_break(self, arg):
        """Set a breakpoint at an address or symbol.

        Usage:
          break *0x1c010746    - break at address
          break 0x1c010746     - break at address
          break main           - break at symbol (requires symbol load)

        The simulation will pause when the PC reaches this address.
        """
        if not arg:
            print("Usage: break <address|symbol>")
            return

        addr = self._resolve_addr(arg)
        if addr is None:
            print(f"Error: cannot resolve '{arg}' (not a valid address or known symbol)")
            return

        # Set on every core: a breakpoint is local to one core, so to catch the PC on whichever
        # core reaches it (FC or any cluster core), it must be inserted on all of them.
        self._cores_cmd(f'breakpoint_insert 0x{addr:x}')

        bp_id = self.next_bp_id
        self.next_bp_id += 1
        self.breakpoints[bp_id] = {'addr': addr}
        print(f"Breakpoint {bp_id} at 0x{addr:08X}")

    def _add_watchpoint(self, arg, wp_type):
        """Add a watchpoint (write or read), optionally scoped to specific masters."""
        parts = arg.strip().split()
        # Optional "on <master>..." restricts the watchpoint to masters whose component path contains
        # one of the given patterns (e.g. dma, ne16, fc, pe0). Default: every master.
        masters = []
        if 'on' in parts:
            idx = parts.index('on')
            masters = parts[idx + 1:]
            parts = parts[:idx]
        if not parts:
            print(f"Usage: {'watch' if wp_type == 'write' else 'rwatch'} <address> [size] [on <master>...]")
            return

        addr = self._resolve_addr(parts[0])
        if addr is None:
            print(f"Error: cannot resolve '{parts[0]}'")
            return

        size = 4  # default 4 bytes
        if len(parts) >= 2:
            try:
                size = int(parts[1], 0)
            except ValueError:
                print(f"Error: invalid size '{parts[1]}'")
                return

        is_write = 1 if wp_type == 'write' else 0
        # Central watchpoint: matched as any master declares its accesses, so it catches every master
        # (cores, cluster DMA, accelerators); the optional master patterns scope it to a subset.
        cmd = f'watchpoint_insert {is_write} 0x{addr:x} {size}'
        if masters:
            cmd += ' ' + ' '.join(masters)
        self.proxy._send_cmd(cmd)

        wp_id = self.next_bp_id
        self.next_bp_id += 1
        self.watchpoints[wp_id] = {'addr': addr, 'size': size, 'type': wp_type, 'masters': masters}
        kind = "Write" if wp_type == 'write' else "Read"
        scope = f" on {', '.join(masters)}" if masters else ""
        print(f"{kind} watchpoint {wp_id} at 0x{addr:08X} (size={size}){scope}")

    def do_watch(self, arg):
        """Set a write watchpoint.

        Usage:
          watch *0x1c000a00         - watch 4 bytes at address
          watch 0x1c000a00 8        - watch 8 bytes at address
          watch my_variable         - watch symbol (requires symbol load)
          watch *0x4000 on ne16     - only when master 'ne16' writes it
          watch buf 8 on dma pe0    - only the cluster DMA or core pe0

        Watchpoints apply to every master (cores, DMA, accelerators) by default; an
        'on <master>...' suffix restricts them to masters whose path contains one of
        the patterns. Stops when the watched address is written.
        """
        self._add_watchpoint(arg, 'write')

    def do_rwatch(self, arg):
        """Set a read watchpoint.

        Usage:
          rwatch *0x1c000a00        - watch 4 bytes at address
          rwatch 0x1c000a00 8       - watch 8 bytes at address
          rwatch *0x4000 on ne16    - only when master 'ne16' reads it

        Applies to every master by default; 'on <master>...' restricts it (see 'watch').
        Stops when the watched address is read.
        """
        self._add_watchpoint(arg, 'read')

    def do_masters(self, arg):
        """List the masters a watchpoint can be scoped to.

        Usage: masters

        Shows the component paths of everything that accesses memory and so can trigger a
        watchpoint (cores, cluster DMA, accelerators, ...). Use any substring of a path as the
        target of 'watch <addr> on <master>'. The list is filled in as masters access memory, so
        run or step the simulation first if it is empty.
        """
        try:
            masters = self.proxy.get_masters()
        except Exception as e:
            print(f"Error: {e}")
            return
        if not masters:
            print("No masters recorded yet (run or step the simulation first).")
            return
        for m in masters:
            print(f"  {m}")

    def do_delete(self, arg):
        """Delete breakpoint(s) or watchpoint(s).

        Usage:
          delete 1      - delete breakpoint/watchpoint 1
          delete        - delete all breakpoints and watchpoints
        """
        if not arg:
            for bp_id, bp in list(self.breakpoints.items()):
                self._cores_cmd(f'breakpoint_remove 0x{bp["addr"]:x}')
            for wp_id, wp in list(self.watchpoints.items()):
                is_write = 1 if wp['type'] == 'write' else 0
                self.proxy._send_cmd(f'watchpoint_remove {is_write} 0x{wp["addr"]:x} {wp["size"]}')
            self.breakpoints.clear()
            self.watchpoints.clear()
            print("All breakpoints and watchpoints deleted.")
            return

        try:
            item_id = int(arg)
        except ValueError:
            print(f"Error: invalid id '{arg}'")
            return

        if item_id in self.breakpoints:
            bp = self.breakpoints.pop(item_id)
            self._cores_cmd(f'breakpoint_remove 0x{bp["addr"]:x}')
            print(f"Breakpoint {item_id} deleted.")
        elif item_id in self.watchpoints:
            wp = self.watchpoints.pop(item_id)
            is_write = 1 if wp['type'] == 'write' else 0
            self.proxy._send_cmd(f'watchpoint_remove {is_write} 0x{wp["addr"]:x} {wp["size"]}')
            print(f"Watchpoint {item_id} deleted.")
        else:
            print(f"Error: no breakpoint or watchpoint {item_id}")

    def do_info(self, arg):
        """Show information.

        Usage:
          info breakpoints   - list all breakpoints and watchpoints
          info registers     - show CPU registers
        """
        parts = arg.split()
        if not parts:
            print("Usage: info breakpoints|registers")
            return

        if parts[0] in ('breakpoints', 'break', 'b', 'watchpoints', 'watch', 'w'):
            if not self.breakpoints and not self.watchpoints:
                print("No breakpoints or watchpoints set.")
                return
            print(f"{'ID':<6} {'Type':<8} {'Address':<14} {'Details'}")
            print("-" * 50)
            for bp_id, bp in sorted(self.breakpoints.items()):
                sym = self._addr_to_symbol(bp['addr']) or ''
                print(f"{bp_id:<6} {'break':<8} 0x{bp['addr']:08X}     {sym}")
            for wp_id, wp in sorted(self.watchpoints.items()):
                kind = 'watch' if wp['type'] == 'write' else 'rwatch'
                masters = wp.get('masters') or []
                scope = f" on {', '.join(masters)}" if masters else ""
                print(f"{wp_id:<6} {kind:<8} 0x{wp['addr']:08X}     size={wp['size']}{scope}")
        elif parts[0] in ('registers', 'reg', 'r'):
            self.do_reg('')
        else:
            print(f"Unknown info subcommand: {parts[0]}")

    def complete_info(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['breakpoints', 'registers', 'watchpoints']
            return [s for s in subcmds if s.startswith(text)]
        return []

    # ──────────────────────────────────────────────
    # Register Access
    # ──────────────────────────────────────────────

    RISCV_REG_NAMES = [
        'zero', 'ra', 'sp', 'gp', 'tp', 't0', 't1', 't2',
        's0', 's1', 'a0', 'a1', 'a2', 'a3', 'a4', 'a5',
        'a6', 'a7', 's2', 's3', 's4', 's5', 's6', 's7',
        's8', 's9', 's10', 's11', 't3', 't4', 't5', 't6',
        'pc'
    ]

    def do_reg(self, arg):
        """Read CPU registers.

        Usage:
          reg              - show all registers
          reg <name>       - show a specific register (e.g. reg sp, reg pc, reg a0)
        """
        result = self._iss_cmd('reg_read')
        if result is None:
            return

        values = result.strip().split()
        if len(values) < 33:
            print(f"Error: unexpected register response ({len(values)} values)")
            return

        regs = {}
        for i, val in enumerate(values[:33]):
            name = self.RISCV_REG_NAMES[i] if i < len(self.RISCV_REG_NAMES) else f'x{i}'
            regs[name] = int(val, 0)
            regs[f'x{i}'] = int(val, 0)

        arg = arg.strip()
        if arg:
            name = arg.lower()
            if name in regs:
                print(f"{name} = 0x{regs[name]:08X} ({regs[name]})")
            else:
                print(f"Unknown register: {arg}")
                print(f"Available: {', '.join(self.RISCV_REG_NAMES)}")
        else:
            # Print all registers in a compact table
            for i in range(0, 32, 4):
                parts = []
                for j in range(4):
                    idx = i + j
                    if idx < 32:
                        name = self.RISCV_REG_NAMES[idx]
                        val = regs[name]
                        parts.append(f"{name:>4} = 0x{val:08X}")
                print('  '.join(parts))
            print(f"  pc = 0x{regs['pc']:08X}")

    # ──────────────────────────────────────────────
    # Symbol Table
    # ──────────────────────────────────────────────

    def _load_symbols(self, path):
        """Load ELF symbols from `path` into the console symbol table.

        Returns (count, error_message); error_message is None on success.
        """
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            return None, f"file not found: {path}"
        try:
            from elftools.elf.elffile import ELFFile
            from elftools.elf.sections import SymbolTableSection
        except ImportError:
            return None, "pyelftools not installed (pip install pyelftools)"
        try:
            with open(path, 'rb') as f:
                elf = ELFFile(f)
                count = 0
                for section in elf.iter_sections():
                    if isinstance(section, SymbolTableSection):
                        for sym in section.iter_symbols():
                            if sym.name and sym.entry['st_value'] != 0 and \
                               sym.entry['st_info']['type'] in ('STT_FUNC', 'STT_OBJECT', 'STT_NOTYPE'):
                                self.symbols[sym.name] = sym.entry['st_value']
                                count += 1
            # Build sorted address list for reverse lookup
            self.sym_addrs = sorted(
                [(addr, name) for name, addr in self.symbols.items()],
                key=lambda x: x[0]
            )
            return count, None
        except Exception as e:
            return None, f"error reading ELF: {e}"

    def _sync_binaries(self):
        """Query the engine's registered binaries and auto-load symbols for any new ones.

        Lets `break <symbol>` work without a manual `symbol load`. Pull-based: the engine accumulates
        binaries (declared by models at reset or dynamically) and we fetch the list here. Already
        loaded ones are skipped, so this is safe to call repeatedly.
        """
        reader = self.proxy.reader if self.proxy else None
        if reader is not None:
            # Record the change-counter we are about to satisfy, so a notification that arrives before
            # this query is not re-handled needlessly.
            self._applied_binaries_seq = reader.binaries_changed_seq
        try:
            binaries = self.proxy.get_binaries()
        except Exception:
            return
        for path in binaries:
            if path in self._loaded_binaries:
                continue
            self._loaded_binaries.add(path)
            if not path or not os.path.isfile(os.path.expanduser(path)):
                continue
            count, err = self._load_symbols(path)
            if not err:
                print(f"Auto-loaded {count} symbols from {path}")

    def _sync_binaries_if_changed(self):
        """Re-sync binaries only when the engine has signalled a change (binaries_changed)."""
        reader = self.proxy.reader if self.proxy else None
        if reader is not None and reader.binaries_changed_seq != self._applied_binaries_seq:
            self._sync_binaries()

    def _addr_to_symbol(self, addr):
        """Resolve an address to the nearest symbol name + offset."""
        if not self.sym_addrs:
            return None
        # Use a sentinel above any real name so every symbol *at* `addr` is included: with a
        # ('', addr) low key, an exact match like (addr, 'main') sorts after (addr, '') and would
        # be skipped, mislabelling the address as the previous symbol + offset.
        idx = bisect.bisect_right(self.sym_addrs, (addr, '\U0010FFFF')) - 1
        if idx < 0:
            return None
        sym_addr, sym_name = self.sym_addrs[idx]
        offset = addr - sym_addr
        if offset == 0:
            return sym_name
        elif offset < 0x10000:  # reasonable offset
            return f"{sym_name}+0x{offset:x}"
        return None

    def _resolve_addr(self, arg):
        """Resolve an argument to an address: hex literal or symbol name."""
        arg = arg.strip().lstrip('*')
        # Try hex/decimal literal first
        try:
            return int(arg, 0)
        except ValueError:
            pass
        # Try symbol lookup
        if arg in self.symbols:
            return self.symbols[arg]
        return None

    def do_symbol(self, arg):
        """Load symbols from an ELF binary.

        Usage:
          symbol load <path>     - load symbol table from ELF file
          symbol lookup <name>   - look up a symbol address
          symbol addr <addr>     - find symbol at address
          symbol list [pattern]  - list symbols (optional grep pattern)
        """
        parts = arg.split(None, 1)
        if not parts:
            print("Usage: symbol load|lookup|addr|list ...")
            return

        subcmd = parts[0]
        subarg = parts[1].strip() if len(parts) > 1 else ''

        if subcmd == 'load':
            if not subarg:
                print("Usage: symbol load <elf_path>")
                return
            count, err = self._load_symbols(subarg)
            if err:
                print(f"Error: {err}")
            else:
                print(f"Loaded {count} symbols from {os.path.expanduser(subarg)}")

        elif subcmd == 'lookup':
            if not subarg:
                print("Usage: symbol lookup <name>")
                return
            if subarg in self.symbols:
                print(f"{subarg} = 0x{self.symbols[subarg]:08X}")
            else:
                # Partial match
                matches = [n for n in self.symbols if subarg in n]
                if matches:
                    for m in sorted(matches)[:20]:
                        print(f"  {m} = 0x{self.symbols[m]:08X}")
                    if len(matches) > 20:
                        print(f"  ... ({len(matches)} total matches)")
                else:
                    print(f"Symbol not found: {subarg}")

        elif subcmd == 'addr':
            if not subarg:
                print("Usage: symbol addr <address>")
                return
            try:
                addr = int(subarg, 0)
            except ValueError:
                print(f"Error: invalid address '{subarg}'")
                return
            sym = self._addr_to_symbol(addr)
            if sym:
                print(f"0x{addr:08X} = {sym}")
            else:
                print(f"No symbol at 0x{addr:08X}")

        elif subcmd == 'list':
            if not self.symbols:
                print("No symbols loaded. Use 'symbol load <elf>'.")
                return
            items = sorted(self.symbols.items())
            if subarg:
                items = [(n, a) for n, a in items if subarg in n]
            for name, addr in items[:50]:
                print(f"  0x{addr:08X}  {name}")
            if len(items) > 50:
                print(f"  ... ({len(items)} total, showing first 50)")

        else:
            print(f"Unknown symbol subcommand: {subcmd}")

    def complete_symbol(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            subcmds = ['load', 'lookup', 'addr', 'list']
            return [s for s in subcmds if s.startswith(text)]
        if len(parts) >= 2 and parts[1] == 'lookup' and self.symbols:
            return [n for n in sorted(self.symbols) if n.startswith(text)][:20]
        return []

    # ──────────────────────────────────────────────
    # Settings
    # ──────────────────────────────────────────────

    def do_set(self, arg):
        """Set console options.

        Usage:
          set router <path>      - set default router path for mem commands
          set iss <path>         - set ISS component path for breakpoints
          set tracefile <path>   - set trace output file for inline display
          set tracefile off      - disable inline trace display
        """
        parts = arg.split(None, 1)
        if len(parts) < 2:
            print("Current settings:")
            print(f"  router    = {self.router_path}")
            print(f"  iss       = {self.iss_path}")
            print(f"  tracefile = {self.trace_file or '(not set)'}")
            return

        option, value = parts[0], parts[1]
        if option == 'router':
            self.router_path = value
            self.router = None  # Force re-creation
            print(f"Router path set to: {value}")
        elif option == 'iss':
            self.iss_path = value
            self.iss_ptr = None  # Force re-resolution
            print(f"ISS path set to: {value}")
        elif option == 'tracefile':
            if value.lower() == 'off':
                self.trace_file = None
                self.trace_file_pos = 0
                print("Inline trace display disabled.")
            else:
                path = os.path.expanduser(value)
                if not os.path.exists(path):
                    print(f"Warning: file '{path}' does not exist yet (will monitor when created)")
                self.trace_file = path
                self._sync_trace_pos()
                print(f"Trace file set to: {path} (position: {self.trace_file_pos})")
        else:
            print(f"Unknown option: {option}")
            print("Available: router, iss, tracefile")

    def complete_set(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            options = ['router', 'iss', 'tracefile']
            return [o for o in options if o.startswith(text)]
        return []

    # ──────────────────────────────────────────────
    # Script Execution
    # ──────────────────────────────────────────────

    def do_script(self, arg):
        """Execute commands from a file.

        Usage: script <filename>
        """
        if not arg:
            print("Usage: script <filename>")
            return
        path = os.path.expanduser(arg.strip())
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}")
            return
        try:
            with open(path) as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    print(f">>> {line}")
                    stop = self.onecmd(line)
                    if stop:
                        break
        except OSError as e:
            print(f"Error reading script: {e}")

    def run_script(self, path):
        """Run a script file (called from entry point with --script)."""
        self.preloop()
        self.do_script(path)
        self.postloop()

    # ──────────────────────────────────────────────
    # Internal Helpers
    # ──────────────────────────────────────────────

    def _step(self, ps, label=None, quiet=False):
        """Step the simulation by ps picoseconds and print the result."""
        reply = self.proxy._send_cmd('step %d' % ps)
        # The reply is the new timestamp
        try:
            timestamp = int(reply.strip())
            self.proxy.reader.timestamp = timestamp
        except ValueError:
            timestamp = None
        self._drain_notifications()
        self._display_new_traces()
        self._update_prompt()
        if not quiet:
            if label is None:
                label = format_time(ps)
            time_str = format_time(timestamp) if timestamp is not None else "unknown"
            print(f"Stepped {label}. Time: {time_str}")
            self._check_breakpoint_hit()

    def _display_new_traces(self):
        """Read and display new lines from the trace file."""
        if self.trace_file is None:
            return
        try:
            with open(self.trace_file, 'r') as f:
                f.seek(self.trace_file_pos)
                new_data = f.read()
                self.trace_file_pos = f.tell()
            if new_data:
                # Strip ANSI color codes for cleaner output
                import re
                clean = re.sub(r'\033\[[0-9;]*m', '', new_data)
                # Print each line with a trace marker
                for line in clean.rstrip('\n').split('\n'):
                    line = line.strip()
                    if line:
                        print(f"  | {line}")
        except (OSError, IOError):
            pass

    def _sync_trace_pos(self):
        """Advance trace file position to end (skip existing content)."""
        if self.trace_file is None:
            return
        try:
            self.trace_file_pos = os.path.getsize(self.trace_file)
        except OSError:
            self.trace_file_pos = 0

    def _on_exit(self, status):
        """Callback when simulation exits (called from reader thread)."""
        self.notifications.put(f"Simulation exited with status {status}")

    def _drain_notifications(self):
        """Print any pending async notifications."""
        while True:
            try:
                msg = self.notifications.get_nowait()
                print(f"\n[!] {msg}")
            except queue.Empty:
                break

    def _update_prompt(self):
        """Update prompt to reflect simulation state."""
        if self.proxy is None:
            self.prompt = "gvsoc> "
            return
        reader = self.proxy.reader
        if reader.sim_has_exited:
            self.prompt = "gvsoc[exited]> "
        elif reader.running:
            self.prompt = "gvsoc[running]> "
        else:
            self.prompt = "gvsoc[stopped]> "


def main():
    """Entry point for the gvconsole command."""
    import argparse

    parser = argparse.ArgumentParser(
        description='GVSoC Interactive Console — connect to a running simulation')
    parser.add_argument("--host", default="localhost",
                        help="Proxy hostname (default: localhost)")
    parser.add_argument("--port", default=42951, type=int,
                        help="Proxy port (default: 42951)")
    parser.add_argument("--script", default=None,
                        help="Execute commands from file then exit")
    parser.add_argument("--tracefile", default=None,
                        help="Trace output file to monitor for inline display")
    parser.add_argument("--gui-embedded", dest="gui_embedded", action="store_true",
                        help="Console runs behind the GUI proxy relay; relay clock-domain selections "
                             "to the timeline icon")
    args = parser.parse_args()

    console = GvsocConsole(args.host, args.port)
    console.gui_embedded = args.gui_embedded
    if args.tracefile:
        console.trace_file = os.path.expanduser(args.tracefile)
        console.trace_file_pos = os.path.getsize(console.trace_file) if os.path.exists(console.trace_file) else 0
    if args.script:
        console.run_script(args.script)
    else:
        try:
            console.cmdloop()
        except KeyboardInterrupt:
            print("\nInterrupted.")
            console.postloop()


if __name__ == '__main__':
    main()
