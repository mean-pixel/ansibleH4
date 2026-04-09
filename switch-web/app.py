import json
import re
import subprocess
from flask import Flask, render_template, request

app = Flask(__name__)

SWITCHES = ["SW01-Mette"]
PORTS = [f"GigabitEthernet1/0/{i}" for i in range(1, 25)]

VALID_SWITCHES = set(SWITCHES)
INTERFACE_REGEX = re.compile(r"^(GigabitEthernet|TenGigabitEthernet)\d+/\d+/\d+$")
DESCRIPTION_REGEX = re.compile(r"^[A-Za-z0-9 _.\-\/]{1,100}$")
ALLOWED_VLANS_REGEX = re.compile(r"^[0-9,\- ]+$")

ANSIBLE_PLAYBOOK = "/usr/bin/ansible-playbook"
ANSIBLE = "/usr/bin/ansible"


def validate_interface(value: str) -> bool:
    return bool(INTERFACE_REGEX.match(value))


def validate_description(value: str) -> bool:
    return bool(DESCRIPTION_REGEX.match(value))


def validate_vlan(value: str) -> bool:
    try:
        vlan = int(value)
        return 1 <= vlan <= 4094
    except Exception:
        return False


def validate_allowed_vlans(value: str) -> bool:
    return bool(value) and bool(ALLOWED_VLANS_REGEX.match(value))


def run_switch_show_commands():
    cmd = [
        ANSIBLE,
        "all",
        "-i", "inventory/hosts.yml",
        "-m", "cisco.ios.ios_command",
        "-a", '{"commands":["show vlan brief","show interfaces trunk"]}',
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


def extract_stdout_blocks(ansible_output):
    """
    Henter 'stdout' blokkene ud af Ansible-output.
    ios_command returnerer typisk noget med "stdout": [...] og "stdout_lines": [...]
    Vi bruger stdout for at få den rå CLI tekst.
    """
    blocks = []

    # Prøv først at finde JSON-lignende stdout arrays
    stdout_match = re.search(r'"stdout":\s*\[(.*?)\]\s*,\s*"stdout_lines"', ansible_output, re.DOTALL)
    if stdout_match:
        raw = "[" + stdout_match.group(1) + "]"
        try:
            parsed = json.loads(raw)
            blocks.extend(parsed)
            return blocks
        except Exception:
            pass

    # fallback: hvis stdout ligger mere "løst" i output
    current_block = []
    capture = False

    for line in ansible_output.splitlines():
        if '"stdout": [' in line:
            capture = True
            continue

        if capture and '"stdout_lines": [' in line:
            if current_block:
                blocks.append("\n".join(current_block))
            break

        if capture:
            cleaned = line.strip().rstrip(",")
            cleaned = cleaned.strip('"')
            cleaned = cleaned.replace("\\n", "\n").replace('\\"', '"')
            if cleaned:
                current_block.append(cleaned)

    return blocks


def parse_vlans(vlan_text):
    vlans = []

    for line in vlan_text.splitlines():
        # eksempel:
        # 7    TEST    active    Gi1/0/10, Gi1/0/24
        match = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s*(.*)$", line)
        if match:
            vlan_id = match.group(1)
            vlan_name = match.group(2)
            status = match.group(3)
            ports = match.group(4).strip()

            # skip gamle reserverede VLANs
            if vlan_id not in ["1002", "1003", "1004", "1005"]:
                vlans.append({
                    "id": vlan_id,
                    "name": vlan_name,
                    "status": status,
                    "ports": ports if ports else "-"
                })

    return vlans


def parse_trunks(trunk_text):
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

            # eksempel:
            # Gi1/0/11  on  802.1q  trunking  1
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

            # eksempel:
            # Gi1/0/11  1,7
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                port = parts[0]
                allowed = parts[1].strip()
                allowed_map[port] = allowed

    for port, data in trunk_map.items():
        data["allowed_vlans"] = allowed_map.get(port, "-")
        trunks.append(data)

    return trunks


def get_switch_state():
    raw_output, error = run_switch_show_commands()
    if error:
        return [], [], error

    stdout_blocks = extract_stdout_blocks(raw_output)

    if len(stdout_blocks) < 2:
        return [], [], "Kunne ikke parse switch-output korrekt."

    vlan_text = stdout_blocks[0]
    trunk_text = stdout_blocks[1]

    vlans = parse_vlans(vlan_text)
    trunks = parse_trunks(trunk_text)

    return vlans, trunks, None


def render_page(output=None):
    vlans, trunks, state_error = get_switch_state()
    return render_template(
        "index.html",
        output=output,
        switches=SWITCHES,
        ports=PORTS,
        vlans=vlans,
        trunks=trunks,
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
