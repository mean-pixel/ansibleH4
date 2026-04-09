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


def render_page(output=None):
    return render_template("index.html", output=output, switches=SWITCHES, ports=PORTS)


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
                "/usr/bin/ansible-playbook",
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
                "/usr/bin/ansible-playbook",
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
