import json
import re
import subprocess
from flask import Flask, render_template, request

app = Flask(__name__)

# Tilpas disse hvis du får flere switches
SWITCHES = ["SW01-Mette"]
PORTS = [f"GigabitEthernet1/0/{i}" for i in range(1, 25)]

VALID_SWITCHES = set(SWITCHES)

INTERFACE_REGEX = re.compile(r"^(GigabitEthernet|TenGigabitEthernet)\d+/\d+/\d+$")
DESCRIPTION_REGEX = re.compile(r"^[A-Za-z0-9ÆØÅæøå _.\-\/]{1,100}$")
VLAN_NAME_REGEX = re.compile(r"^[A-Za-z0-9ÆØÅæøå _.\-\/]{1,100}$")
ALLOWED_VLANS_REGEX = re.compile(r"^[0-9,\- ]+$")

ANSIBLE_PLAYBOOK = "/usr/bin/ansible-playbook"
ANSIBLE = "/usr/bin/ansible"


def validate_interface(value: str) -> bool:
    return bool(INTERFACE_REGEX.fullmatch(value))


def validate_description(value: str) -> bool:
    return bool(DESCRIPTION_REGEX.fullmatch(value))


def validate_vlan_name(value: str) -> bool:
    if value == "":
        return True
    return bool(VLAN_NAME_REGEX.fullmatch(value))


def validate_vlan(value: str) -> bool:
    try:
        vlan = int(value)
        return 1 <= vlan <= 4094
    except (TypeError, ValueError):
        return False


def validate_allowed_vlans(value: str) -> bool:
    return bool(value) and bool(ALLOWED_VLANS_REGEX.fullmatch(value))


def run_switch_show_commands():
    cmd = [
        ANSIBLE,
        "all",
        "-i", "inventory/hosts.yml",
        "-m", "cisco.ios.ios_command",
        "-a",
        '{"commands":["show vlan brief","show interfaces trunk","show running-config | section ^interface"]}',
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            check=False
        )

        if result.returncode != 0:
            return None, f"Kunne ikke hente switch-status:\n{result.stderr}"

        return result.stdout, None

    except subprocess.TimeoutExpired:
        return None, "Timeout ved hentning af switch-status."
    except Exception as exc:
        return None, f"Fejl ved hentning af switch-status: {exc}"


def extract_stdout_blocks(ansible_output: str):
    blocks = []

    stdout_match = re.search(
        r'"stdout":\s*(\[[\s\S]*?\])\s*,\s*"stdout_lines"',
        ansible_output,
        re.DOTALL
    )

    if not stdout_match:
        return blocks

    raw = stdout_match.group(1)

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            blocks.extend(parsed)
    except Exception:
        pass

    return blocks


def parse_vlans(vlan_text: str):
    vlans = []

    for line in vlan_text.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s*(.*)$", line)
        if match:
            vlan_id = match.group(1)
            vlan_name = match.group(2)
            status = match.group(3)
            ports = match.group(4).strip()

            if vlan_id not in {"1002", "1003", "1004", "1005"}:
                vlans.append({
                    "id": vlan_id,
                    "name": vlan_name,
                    "status": status,
                    "ports": ports if ports else "-"
                })

    return vlans


def parse_active_trunks(trunk_text: str):
    trunks = []
    lines = trunk_text.splitlines()

    main_table = False
    allowed_table = False

    trunk_map = {}
    allowed_map = {}

    for line in lines:
        stripped = line.strip()

        if "Port" in line and "Mode" in line and "Encapsulation" in line:
            main_table = True
            allowed_table = False
            continue

        if "Port" in line and "Vlans allowed on trunk" in line:
            main_table = False
            allowed_table = True
            continue

        if main_table:
            if not stripped or stripped.startswith("Port"):
                continue

            parts = stripped.split()
            if len(parts) >= 5:
                port = parts[0]
                mode = parts[1]
                encapsulation = parts[2]
                status = parts[3]
                native_vlan = parts[4]

                trunk_map[port] = {
                    "port": port,
                    "mode": mode,
                    "encapsulation": encapsulation,
                    "status": status,
                    "native_vlan": native_vlan,
                }

        if allowed_table:
            if not stripped or stripped.startswith("Port"):
                continue

            parts = stripped.split(None, 1)
            if len(parts) == 2:
                port = parts[0]
                allowed = parts[1].strip()
                allowed_map[port] = allowed

    for port, data in trunk_map.items():
        data["allowed_vlans"] = allowed_map.get(port, "-")
        trunks.append(data)

    return trunks


def parse_configured_trunks(config_text: str):
    configured_trunks = []

    blocks = re.split(r"\n(?=interface )", config_text)

    for block in blocks:
        block = block.strip()
        if not block.startswith("interface "):
            continue

        lines = block.splitlines()
        interface_name = lines[0].replace("interface ", "").strip()

        mode = None
        allowed_vlans = "-"
        description = "-"

        for line in lines[1:]:
            stripped = line.strip()

            if stripped.startswith("description "):
                description = stripped.replace("description ", "", 1).strip()

            if stripped == "switchport mode trunk":
                mode = "trunk"

            if stripped.startswith("switchport trunk allowed vlan "):
                allowed_vlans = stripped.replace("switchport trunk allowed vlan ", "", 1).strip()

        if mode == "trunk":
            configured_trunks.append({
                "port": interface_name,
                "description": description,
                "allowed_vlans": allowed_vlans
            })

    return configured_trunks


def get_switch_state():
    raw_output, error = run_switch_show_commands()
    if error:
        return [], [], [], error

    stdout_blocks = extract_stdout_blocks(raw_output)

    if len(stdout_blocks) < 3:
        return [], [], [], "Kunne ikke parse switch-output korrekt."

    vlan_text = stdout_blocks[0]
    trunk_text = stdout_blocks[1]
    config_text = stdout_blocks[2]

    vlans = parse_vlans(vlan_text)
    active_trunks = parse_active_trunks(trunk_text)
    configured_trunks = parse_configured_trunks(config_text)

    return vlans, configured_trunks, active_trunks, None


def render_page(output=None):
    vlans, configured_trunks, active_trunks, state_error = get_switch_state()

    return render_template(
        "index.html",
        output=output,
        switches=SWITCHES,
        ports=PORTS,
        vlans=vlans,
        configured_trunks=configured_trunks,
        active_trunks=active_trunks,
        state_error=state_error,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    output = None

    if request.method == "POST":
        switch = request.form.get("switch", "").strip()
        interface = request.form.get("interface", "").strip()
        description = request.form.get("description", "").strip()
        mode = request.form.get("mode", "").strip()
        vlan_name = request.form.get("vlan_name", "").strip()

        if switch not in VALID_SWITCHES:
            return render_page("Fejl: Ugyldig switch.")

        if interface not in PORTS:
            return render_page("Fejl: Ugyldig port.")

        if not validate_interface(interface):
            return render_page("Fejl: Ugyldigt interface-format.")

        if not validate_description(description):
            return render_page("Fejl: Ugyldig description.")

        if not validate_vlan_name(vlan_name):
            return render_page("Fejl: Ugyldigt VLAN-navn.")

        if mode == "access":
            vlan_id = request.form.get("vlan_id", "").strip()

            if not validate_vlan(vlan_id):
                return render_page("Fejl: Ugyldigt VLAN-id.")

            cmd = [
                ANSIBLE_PLAYBOOK,
                "-i", "inventory/hosts.yml",
                "playbooks/set_access_port.yml",
                "-e", f"switch={switch}",
                "-e", f"interface={interface}",
                "-e", f"vlan_id={vlan_id}",
                "-e", f"description={description}",
            ]

            if vlan_name:
                cmd.extend(["-e", f"vlan_name={vlan_name}"])

        elif mode == "trunk":
            allowed_vlans = request.form.get("allowed_vlans", "").strip()

            if not validate_allowed_vlans(allowed_vlans):
                return render_page("Fejl: Ugyldig allowed VLAN-liste.")

            cmd = [
                ANSIBLE_PLAYBOOK,
                "-i", "inventory/hosts.yml",
                "playbooks/set_trunk_port.yml",
                "-e", f"switch={switch}",
                "-e", f"interface={interface}",
                "-e", f"description={description}",
                "-e", f"allowed_vlans={allowed_vlans}",
            ]
        else:
            return render_page("Fejl: Ugyldig mode.")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

            output = (
                f"EXIT CODE: {result.returncode}\n\n"
                f"STDOUT:\n{result.stdout}\n\n"
                f"STDERR:\n{result.stderr}"
            )

        except subprocess.TimeoutExpired:
            output = "Fejl: Ansible-jobbet tog for lang tid."
        except Exception as exc:
            output = f"Fejl under kørsel: {exc}"

    return render_page(output)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
