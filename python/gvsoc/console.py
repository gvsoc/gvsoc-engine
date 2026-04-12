# SPDX-FileCopyrightText: 2026 ETH Zurich and University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

"""GVSoC Interactive Console — a REPL for controlling a running simulation."""

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
        self._update_prompt()
        return line

    def postcmd(self, stop, line):
        """Update prompt after each command."""
        self._drain_notifications()
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
        if not arg:
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

        # Use true cycle-based stepping
        reply = self.proxy.step_cycles(clock_idx, count)
        try:
            timestamp = int(reply.strip())
            self.proxy.reader.timestamp = timestamp
        except ValueError:
            timestamp = None
        self._drain_notifications()
        self._update_prompt()
        time_str = format_time(timestamp) if timestamp is not None else "unknown"
        print(f"Stepped {count} cycle(s). Time: {time_str}")

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
    # Settings
    # ──────────────────────────────────────────────

    def do_set(self, arg):
        """Set console options.

        Usage:
          set router <path>   - set default router path for mem commands
        """
        parts = arg.split(None, 1)
        if len(parts) < 2:
            print("Current settings:")
            print(f"  router = {self.router_path}")
            return

        option, value = parts[0], parts[1]
        if option == 'router':
            self.router_path = value
            self.router = None  # Force re-creation
            print(f"Router path set to: {value}")
        else:
            print(f"Unknown option: {option}")
            print("Available: router")

    def complete_set(self, text, line, begidx, endidx):
        parts = line.split()
        if len(parts) == 2 or (len(parts) == 1 and not text):
            options = ['router']
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

    def _step(self, ps, label=None):
        """Step the simulation by ps picoseconds and print the result."""
        reply = self.proxy._send_cmd('step %d' % ps)
        # The reply is the new timestamp
        try:
            timestamp = int(reply.strip())
            self.proxy.reader.timestamp = timestamp
        except ValueError:
            timestamp = None
        self._drain_notifications()
        self._update_prompt()
        if label is None:
            label = format_time(ps)
        time_str = format_time(timestamp) if timestamp is not None else "unknown"
        print(f"Stepped {label}. Time: {time_str}")

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
    args = parser.parse_args()

    console = GvsocConsole(args.host, args.port)
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
