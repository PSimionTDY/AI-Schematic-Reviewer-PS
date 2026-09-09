#!/usr/bin/env python3
"""
Connection Finder - Quick search utility for schematic connections
Helps you find components, nets, and trace connection chains
"""

import os
import re
from pathlib import Path


class ConnectionFinder:
    def __init__(self, report_file):
        self.report_file = report_file
        with open(report_file, 'r', encoding='utf-8') as f:
            self.content = f.read()
            self.lines = self.content.split('\n')
    
    def find_component(self, component_name):
        """Find all connections for a component."""
        pattern = f"^### {re.escape(component_name)}$"
        print(f"\n{'='*70}")
        print(f"CONNECTIONS FOR COMPONENT: {component_name}")
        print(f"{'='*70}\n")
        
        found = False
        for i, line in enumerate(self.lines):
            if re.match(pattern, line):
                found = True
                # Print the component section (next 100 lines or until next component)
                j = i
                pin_count = 0
                while j < len(self.lines) and pin_count < 200:
                    if j > i and self.lines[j].startswith("### "):
                        break
                    if self.lines[j].strip():
                        print(self.lines[j])
                        if " connected to " in self.lines[j]:
                            pin_count += 1
                    j += 1
                break
        
        if not found:
            print(f"Component '{component_name}' not found in report")
        return found
    
    def find_net(self, net_name):
        """Find all connections for a specific net/signal."""
        # Handle special characters in net names
        safe_net = net_name.replace('(', r'\(').replace(')', r'\)')
        pattern = f"^## Net: `{safe_net}`$"
        
        print(f"\n{'='*70}")
        print(f"CONNECTIONS ON NET: {net_name}")
        print(f"{'='*70}\n")
        
        found = False
        for i, line in enumerate(self.lines):
            if re.match(pattern, line):
                found = True
                # Print the net section (until next net)
                j = i
                count = 0
                while j < len(self.lines) and count < 500:
                    if j > i and self.lines[j].startswith("## Net: "):
                        break
                    if self.lines[j].strip():
                        print(self.lines[j])
                        if "| " in self.lines[j] and "Component" not in self.lines[j]:
                            count += 1
                    j += 1
                break
        
        if not found:
            print(f"Net '{net_name}' not found in report")
        return found
    
    def find_power_nets(self):
        """Find all major power nets."""
        print(f"\n{'='*70}")
        print("MAJOR POWER NETS")
        print(f"{'='*70}\n")
        
        power_nets = [
            '12V_ORED',
            '3V3',
            '1V8', '1V8A',
            '1V2', '1V2A',
            '0V95', '0V9A',
            'GND'
        ]
        
        found_nets = []
        for net in power_nets:
            if f"## Net: `{net}`" in self.content or f"connected to `{net}`" in self.content:
                found_nets.append(net)
                print(f"  [OK] {net}")
        
        return found_nets
    
    def find_memory_nets(self):
        """Find DDR4 memory nets."""
        print(f"\n{'='*70}")
        print("DDR4 MEMORY NETS (sample)")
        print(f"{'='*70}\n")
        
        # Find DDR4 net patterns
        pattern = r"`(DDR4_\d\.[\w/]+)`"
        nets = set(re.findall(pattern, self.content))
        
        ddr4_groups = {'DDR4_0': [], 'DDR4_1': [], 'DDR4_2': [], 'DDR4_3': []}
        for net in sorted(nets):
            for group in ddr4_groups:
                if net.startswith(group):
                    ddr4_groups[group].append(net)
                    break
        
        for group, nets in ddr4_groups.items():
            if nets:
                print(f"\n{group}: ({len(nets)} signals)")
                # Show first 10
                for net in nets[:10]:
                    print(f"  - {net}")
                if len(nets) > 10:
                    print(f"  ... and {len(nets)-10} more")
        
        return ddr4_groups
    
    def find_ic_by_prefix(self, prefix):
        """Find all ICs with a given prefix."""
        pattern = f"^### {prefix}\\d+"
        print(f"\n{'='*70}")
        print(f"COMPONENTS STARTING WITH '{prefix}'")
        print(f"{'='*70}\n")
        
        components = []
        for line in self.lines:
            if re.match(pattern, line):
                comp = line.replace("### ", "").strip()
                components.append(comp)
        
        print(f"Found {len(components)} components:\n")
        for comp in sorted(components)[:20]:
            print(f"  {comp}")
        
        if len(components) > 20:
            print(f"\n  ... and {len(components)-20} more")
        
        return components
    
    def trace_connection_chain(self, start_component, start_pin):
        """Trace a connection through the circuit."""
        print(f"\n{'='*70}")
        print(f"TRACING CONNECTION CHAIN")
        print(f"Starting from: {start_component} Pin {start_pin}")
        print(f"{'='*70}\n")
        
        # Find the net connected to this pin
        pattern = f"### {re.escape(start_component)}$"
        for i, line in enumerate(self.lines):
            if re.match(pattern, line):
                # Look for the pin
                for j in range(i, min(i+500, len(self.lines))):
                    if f"Pin **{start_pin}**" in self.lines[j]:
                        # Extract net name from this line
                        match = re.search(r"`([^`]+)`", self.lines[j])
                        if match:
                            net_name = match.group(1)
                            print(f"Found net: {net_name}")
                            print(f"\nAll components on this net:\n")
                            
                            # Find all components on this net
                            self.find_net(net_name)
                            return net_name
                        break
        
        print(f"Could not find pin {start_pin} on component {start_component}")
        return None


def interactive_menu(finder):
    """Interactive menu for searching connections."""
    while True:
        print(f"\n{'='*70}")
        print("CONNECTION FINDER - Main Menu")
        print(f"{'='*70}")
        print("""
1. Find component connections
2. Find net/signal connections
3. List major power nets
4. List DDR4 memory nets
5. Find ICs by prefix (U, R, C, etc.)
6. Trace connection chain
7. Exit
        """)
        
        choice = input("Select option (1-7): ").strip()
        
        if choice == '1':
            comp = input("Enter component name (e.g., U400, R512): ").strip()
            finder.find_component(comp)
        
        elif choice == '2':
            net = input("Enter net name (e.g., 12V_ORED, GND): ").strip()
            finder.find_net(net)
        
        elif choice == '3':
            finder.find_power_nets()
        
        elif choice == '4':
            finder.find_memory_nets()
        
        elif choice == '5':
            prefix = input("Enter prefix (U, R, C, L, J, X, H): ").strip()
            finder.find_ic_by_prefix(prefix)
        
        elif choice == '6':
            comp = input("Component (e.g., U400): ").strip()
            pin = input("Pin number (e.g., A1, P1): ").strip()
            finder.trace_connection_chain(comp, pin)
        
        elif choice == '7':
            print("Exiting...")
            break
        
        else:
            print("Invalid option, please try again")
        
        input("\nPress Enter to continue...")


def main():
    # Locate CONNECTIONS_REPORT.md relative to this script's repo root
    _here = Path(__file__).resolve()
    # Walk up to find the git root (contains .git)
    repo_root = _here
    for _ in range(10):
        if (repo_root / ".git").exists():
            break
        repo_root = repo_root.parent
    report_file = str(repo_root / "CONNECTIONS_REPORT.md")
    
    if not os.path.exists(report_file):
        print(f"Error: Report file not found: {report_file}")
        print("Run: python schematic_parser.py")
        return
    
    print(f"\nLoading report: {report_file}")
    finder = ConnectionFinder(report_file)
    print("Report loaded successfully\n")
    
    # Check for command line arguments
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "-c" and len(sys.argv) > 2:
            # Find component
            finder.find_component(sys.argv[2])
        elif sys.argv[1] == "-n" and len(sys.argv) > 2:
            # Find net
            finder.find_net(sys.argv[2])
        elif sys.argv[1] == "-p":
            # Power nets
            finder.find_power_nets()
        elif sys.argv[1] == "-m":
            # Memory nets
            finder.find_memory_nets()
        elif sys.argv[1] == "-t" and len(sys.argv) > 3:
            # Trace
            finder.trace_connection_chain(sys.argv[2], sys.argv[3])
        else:
            print(f"""
Usage:
  python connection_finder.py                    - Interactive menu
  python connection_finder.py -c <component>    - Find component (e.g., -c U400)
  python connection_finder.py -n <net>          - Find net (e.g., -n 12V_ORED)
  python connection_finder.py -p                - Show power nets
  python connection_finder.py -m                - Show DDR4 memory nets
  python connection_finder.py -t <comp> <pin>   - Trace connection (e.g., -t U400 A1)
            """)
    else:
        # Interactive mode
        interactive_menu(finder)


if __name__ == "__main__":
    main()
