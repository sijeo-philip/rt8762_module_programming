# Station and Golden Rig — Step-by-Step Guide

For **Operators**, **Supervisors** and the **technician** who sets up the line.

Each step says who does it:

| Badge | Who |
|---|---|
| 🟢 **Operator** | Runs batches: loads modules, presses buttons, scans QR codes. |
| 🔵 **Supervisor** | Opens the shift, binds the jig and Golden Rig, handles problems. |
| 🟠 **Technician** | One-time setup: installs software, creates certificates. Usually IT or engineering. |

> **Rule of thumb:** if the screen says **"Call the Supervisor"**, or shows something red that this guide does not explain, stop and call the Supervisor. Never unplug cables or move modules while the screen says *"Do not remove modules"*.

---

## Contents

- [Words used in this guide](#words-used-in-this-guide)
- **Part A — One-time certificate setup (Technician)**
  - [A1. What the certificates are for](#a1-what-the-certificates-are-for)
  - [A2. Create the certificates](#a2-create-the-certificates)
  - [A3. Hand out the files](#a3-hand-out-the-files)
- **Part B — Golden Rig**
  - [B1. First-time setup of the rig (Technician)](#b1-first-time-setup-of-the-rig-technician)
  - [B2. Every day on the rig (Operator / Supervisor)](#b2-every-day-on-the-rig-operator--supervisor)
  - [B3. Changes to the rig that need re-approval](#b3-changes-to-the-rig-that-need-re-approval)
- **Part C — Station app**
  - [C1. First-time station setup (Technician)](#c1-first-time-station-setup-technician)
  - [C2. Logging in](#c2-logging-in)
  - [C3. Bind jig slots to COM ports (Supervisor)](#c3-bind-jig-slots-to-com-ports-supervisor)
  - [C4. Bind the Golden Rig — first time (Supervisor)](#c4-bind-the-golden-rig--first-time-supervisor)
  - [C5. Production settings (Supervisor)](#c5-production-settings-supervisor)
  - [C6. Start of every shift — open the session (Supervisor)](#c6-start-of-every-shift--open-the-session-supervisor)
  - [C7. Normal production — running a batch (Operator)](#c7-normal-production--running-a-batch-operator)
  - [C8. Golden Rig — after the first time (Supervisor)](#c8-golden-rig--after-the-first-time-supervisor)
  - [C9. End of shift (Supervisor)](#c9-end-of-shift-supervisor)
- [D. Problems and what to do](#d-problems-and-what-to-do)
- [E. Checklists](#e-checklists)
- [F. Bench tool: test the whole flow without the LAN server](#f-bench-tool-test-the-whole-flow-without-the-lan-server)

---

## Words used in this guide

| Word | Meaning |
|---|---|
| **Station** | The PC running the Station app, with the jig connected to it. |
| **Jig** | The fixture that holds 4 or 8 modules. Each position is a **slot**, numbered 1, 2, 3… |
| **COM port** | The USB-to-serial cable or adapter that connects one slot to the PC. Windows names it COM4, COM6, and so on. |
| **Slot binding** | Telling the station which USB adapter belongs to which slot. It is done once and remembered. |
| **Golden Rig** | A separate PC with a reference Bluetooth module. It checks that every programmed module transmits correctly (the **RF test**). Several stations can share one rig. |
| **Rig name** | The rig's network name, for example `golden-rig-01.local`. A name ending in **`.local`** is an **mDNS name**: the rig announces it on the local network by itself, so no IT server is needed. |
| **IP address** | The rig's number on the network, for example `192.168.10.50`. It can change when the rig restarts, unless IT fixes it. |
| **Certificate** | A digital ID card for the Golden Rig. The station uses it to make sure it is talking to the real, approved rig. |
| **Fingerprint (SHA-256)** | A long code of letters and digits that is unique to one certificate. If the rig's certificate changes, its fingerprint changes. |
| **Session** | The Supervisor opens a production session at the start of a shift. Operators can run batches only while a session is open. |

---

# Part A — One-time certificate setup (Technician)

This part is done **once for the whole line**, before any station is bound to a rig. Operators and Supervisors do not need it. They only need to know that the files exist (see [A3](#a3-hand-out-the-files)).

## A1. What the certificates are for

```
             Certificate Authority (CA) — created once, kept safe offline
                   │ signs                              │ public copy goes to
                   ▼                                    ▼
   Golden Rig: rig.pem + rig.key              Every station:
   ("I am GOLDEN-RIG-01,                      golden_rig_ca.pem
    reachable as golden-rig-01.local")        ("trust rigs signed by our CA")
```

- The **Golden Rig** has its own certificate (`rig.pem`) and a secret key (`rig.key`).
- The **station does not have a certificate of its own.** It only needs a public copy of the CA certificate (`golden_rig_ca.pem`). With it, the station can check that a rig's certificate was issued by your company.
- The **CA key** (`golden_rig_ca.key`) is the master key. Anyone who has it can make a fake rig, so keep it offline (for example on an encrypted USB stick in a locked cabinet). It never goes on a station or a rig.

## A2. Create the certificates

Use any Windows PC with **Git for Windows** installed; it includes the `openssl` tool. Work in a new, empty folder, for example `C:\rig-certs`.

### Step 1 — Choose the rig's ID and name

Write these down before you start:

| Item | Example | Rule |
|---|---|---|
| **Rig ID** | `GOLDEN-RIG-01` | Unique per rig. Shown to Supervisors on every station. |
| **Rig name (mDNS)** | `golden-rig-01.local` | The rig PC's computer name + `.local`. Letters, digits and `-` only. **Recommended.** |
| **Fixed IP (optional)** | `192.168.10.50` | Only if IT gives the rig a fixed IP. Leave it out otherwise. |

> **Why the name matters:** the station checks that the name or IP it connects to is written inside the rig's certificate. If Supervisors will type `golden-rig-01.local`, that name must be in the certificate. If they might type the IP, the IP must be in it too.

### Step 2 — Open Git Bash in the folder

Right-click inside `C:\rig-certs` → **Open Git Bash here**. Copy and paste the following commands one block at a time.

### Step 3 — Create the CA (once for the whole line)

Skip this step if your line already has a CA. Reuse the existing `golden_rig_ca.pem` and `golden_rig_ca.key`.

```bash
MSYS_NO_PATHCONV=1 openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout golden_rig_ca.key -out ca.csr -subj "/CN=Golden Rig CA"

cat > ca.ext <<'EOF'
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
EOF

openssl x509 -req -in ca.csr -signkey golden_rig_ca.key -days 3650 -sha256 \
  -extfile ca.ext -out golden_rig_ca.pem
```

Result: `golden_rig_ca.pem` (public, goes to stations) and `golden_rig_ca.key` (secret, keep offline). The CA is valid for 10 years.

### Step 4 — Create the rig's certificate (once per rig)

Replace `GOLDEN-RIG-01`, `golden-rig-01.local` and the IP with your values from Step 1. If you have no fixed IP, use `subjectAltName=DNS:golden-rig-01.local`.

```bash
MSYS_NO_PATHCONV=1 openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout rig.key -out rig.csr -subj "/CN=GOLDEN-RIG-01"

cat > rig.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:golden-rig-01.local
EOF

openssl x509 -req -in rig.csr -CA golden_rig_ca.pem -CAkey golden_rig_ca.key \
  -CAcreateserial -days 1095 -sha256 -extfile rig.ext -out rig.pem
```

The rig certificate is valid for 3 years (1095 days). Put the expiry date in the maintenance calendar: renewing it means re-approving the rig on every station (see [B3](#b3-changes-to-the-rig-that-need-re-approval)).

### Step 5 — Check the result and note the fingerprint

```bash
openssl verify -CAfile golden_rig_ca.pem rig.pem
openssl x509 -in rig.pem -noout -subject -ext subjectAltName -enddate -fingerprint -sha256
```

- The first command must print `rig.pem: OK`.
- The second shows the rig ID (CN), the names and IPs, the expiry date, and the **SHA-256 fingerprint**.
- **Write the fingerprint down** and give it to the Supervisors. When they bind the rig, they compare it with the one on screen ([C4](#c4-bind-the-golden-rig--first-time-supervisor), step 6).

> Do **not** create the CA with `openssl req -x509 -addext …`. On some OpenSSL versions, it makes a CA that the station rejects with *"unable to get local issuer certificate"*. Use the commands above exactly.

## A3. Hand out the files

| File | Goes to | Secret? |
|---|---|---|
| `golden_rig_ca.pem` | **Every station**, at `config\certificates\golden_rig_ca.pem` inside the StationApp folder | No (public) |
| `rig.pem` | That **Golden Rig** only | No |
| `rig.key` | That **Golden Rig** only | **Yes.** Readable only by the rig service account. |
| `golden_rig_ca.key` | **Nowhere.** Kept offline. | **Yes, most secret.** |
| `ca.csr`, `rig.csr`, `*.ext`, `*.srl` | Can be deleted | — |
| Fingerprint + rig ID + rig name | Supervisors (on paper or by email) | No |

---

# Part B — Golden Rig

The Golden Rig runs the **Golden Rig application** described in [golden_rig_application.md](golden_rig_application.md). It has no screens for Operators. It runs in the background and answers the stations.

## B1. First-time setup of the rig (Technician)

1. **Give the rig PC its name.**
   - Windows: *Settings → System → About → Rename this PC* → enter `golden-rig-01` (without `.local`) → restart.
   - Windows 10 and 11 announce the name as `golden-rig-01.local` on the network automatically.
   - Linux: set the hostname and make sure the `avahi-daemon` service is running.
2. **Connect it to the same network as the stations** (same switch / subnet). mDNS names only work inside one local network. If the rig is on a different subnet, ask IT for a fixed IP or a DNS name instead, and put it in the certificate.
3. **Copy the certificate files** `rig.pem` and `rig.key` (from [A2](#a2-create-the-certificates)) to the rig, for example to `C:\GoldenRig\certs\`. Make `rig.key` readable only by the account the rig service runs as.
4. **Install and start the rig application** with its **rig ID** (it must be exactly the certificate's CN):
   ```
   python golden_rig_server.py --rig-id GOLDEN-RIG-01 --cert C:\GoldenRig\certs\rig.pem --key C:\GoldenRig\certs\rig.key --port 8762
   ```
5. **Make it start automatically** at boot: on Windows with NSSM or a scheduled task; on Linux with a `systemd` unit set to `Restart=always`.
6. **Open the firewall** on the rig:
   - **TCP 8762, inbound**, from the station network (the stations' test requests).
   - **UDP 5353**, inbound and outbound (mDNS, so stations can find the rig by name). On Windows, set the network profile to *Private*: mDNS is normally allowed there.
7. **Check from a station PC.** Open a Command Prompt and run:
   ```
   ping golden-rig-01.local
   ```
   It should answer with the rig's IP. Then do a real check from the Station app: [C4](#c4-bind-the-golden-rig--first-time-supervisor), steps 1–5.
8. **Turn on time sync (NTP)** on the rig, and keep its logs for at least 90 days.

## B2. Every day on the rig (Operator / Supervisor)

There is nothing to do on the rig itself in normal production.

- **Keep the rig powered on and connected** to the network whenever any station is producing.
- **Do not rename the rig PC, replace its certificate, or change its port.** Each of these stops every station from using it until a Supervisor re-approves it.
- **If stations show a Golden Rig error**, check that the rig PC is on and its network cable is connected. Restarting the rig PC is safe: the service starts again by itself, and if the rig is bound by name, a new IP does not matter.
- **To check that the rig answers** without testing a module: a Supervisor uses *Request identity* on any station (see [C8](#c8-golden-rig--after-the-first-time-supervisor)).

## B3. Changes to the rig that need re-approval

| Change on the rig | What happens on stations | What to do |
|---|---|---|
| IP address changes, rig bound **by name** | Nothing. Stations find it by name. | Nothing. |
| IP address changes, rig bound **by IP** | RF tests and session start fail ("Cannot reach …"). | Supervisor re-binds with the new IP or, better, by name ([C8.3](#c83-the-rigs-address-or-certificate-changed)). The new IP must be in the certificate. |
| Certificate renewed or replaced | Session start shows *"presents a different certificate than the approved one"*. | Supervisor re-approves on **every** station ([C8.3](#c83-the-rigs-address-or-certificate-changed)). |
| Rig renamed | Stations cannot find it, or the certificate no longer matches. | Technician makes a new certificate with the new name ([A2](#a2-create-the-certificates), step 4). Supervisor re-approves on every station. |
| Rig replaced by another PC | As above. | New certificate with a new rig ID (and add it to the approved inventory if one is used). Supervisor approves on every station. |
| Port changed | Stations cannot connect. | Supervisor re-binds with the new port on every station. |

---

# Part C — Station app

## C1. First-time station setup (Technician)

Do this once per station PC.

1. Install StationApp in its folder (for example `D:\StationApp`) and create a desktop shortcut that starts it from that folder.
2. Copy `golden_rig_ca.pem` (from [A3](#a3-hand-out-the-files)) to `config\certificates\golden_rig_ca.pem` inside the StationApp folder.
3. In the station's `.env` settings file, check at least:

   | Setting | Example | Meaning |
   |---|---|---|
   | `STATION_ID`, `JIG_ID` | `STN-01`, `JIG-01` | Unique per station / jig. Never reuse them. |
   | `JIG_POSITIONS` | `8` | 4 or 8 slots. |
   | `MPCLI_PATH` | path to `mpcli.exe` | Programming tool. |
   | `LAN_SERVER_URL`, `LAN_SERVER_API_KEY` | from IT | Logins, MAC addresses and uploads. |
   | `AUTH_MODE` | `server` | **Must be `server` in production.** `test` is for testing only. |
   | `GOLDEN_RIG_CA_CERTIFICATE` | `./config/certificates/golden_rig_ca.pem` | The CA file from step 2. |
   | `GOLDEN_RIG_INVENTORY` | `GOLDEN-RIG-01,GOLDEN-RIG-02` | Optional. Only these rig IDs can be approved. Empty = any rig signed by your CA. |
   | `GOLDEN_RIG_CANDIDATES` | `golden-rig-01.local:8762` | Optional. Rigs listed under *Discover rigs*, so Supervisors do not have to type them. |
   | `SIMULATION_MODE` | `false` | **Must be `false` in production.** |

4. Plug the USB QR / badge scanner into the station PC. Both scanner modes work:
   - **HID POS mode:** Device Manager shows *POS HID Barcode scanner*. The app reads it through Windows. Install the requirements with `pip install -r requirements.txt` (this includes the `winrt-…` packages), and keep `QR_SCANNER_HID_POS=true`.
   - **Keyboard mode:** the scanner types the code. The cursor must be in the scan box.
5. Make sure the Operator and Supervisor accounts are **enrolled on the LAN server**. Accounts are not created on the station: anyone enrolled can log in on any station.

> If the app shows a yellow **SIMULATION MODE** banner, or the login screen says it is using **test users**, the station is **not** set up for production. Do not produce; call the technician.

## C2. Logging in

When the app starts, it shows the **login screen** with the station name.

**To log in with a badge** 🟢🔵
1. Click in the **"Scan your badge"** box (it is usually selected already).
2. Scan your badge.
   - **Operators** are logged in straight away.
   - **Supervisors** are asked for their password as well.

**To log in with your user ID** 🟢🔵
1. Type your **User ID** and **Password**.
2. Press **LOG IN**.

**What you see afterwards**
- **Operator:** a simple production screen with the jig slots, progress and results only.
- **Supervisor:** the full screen with tabs: *Production*, *Production settings*, *Station configuration* and *Station status*.

**Supervisor help while an Operator is logged in** 🔵
1. Press **Supervisor** in the top bar and log in. The full screen opens.
2. When finished, press **Log out Supervisor**. The Operator stays logged in.

The Supervisor login lapses on its own after 15 minutes of no Supervisor use. After 5 wrong passwords an account is locked for 5 minutes.

## C3. Bind jig slots to COM ports (Supervisor)

**Why:** each slot of the jig is wired to the PC through its own USB-to-serial adapter. Windows gives these adapters COM numbers (COM4, COM6, …), but the numbers can change when cables are re-plugged. The station therefore remembers each adapter by its **own fixed USB identity** (its serial number or the USB socket it is in), not by the COM number. You bind each slot once, and the station finds it again even if Windows renumbers the ports.

**When to do it:**
- When the station is set up for the first time.
- When a USB adapter or cable is **replaced**.
- When the jig is plugged into **different USB sockets** or a different hub.
- When a slot shows **NOT DETECTED**.

**Before you start:**
- No batch may be running.
- Have a list of which adapter or cable goes to which slot. If you are not sure, label each cable with its slot number first.

**Steps**

1. Log in as Supervisor. Open **Station configuration → Jig slot binding**.
2. The table shows every slot:

   | Column | Meaning |
   |---|---|
   | **Slot** | The physical position on the jig. |
   | **Bound USB identity** | The adapter remembered for that slot, or **unbound**. |
   | **Currently detected as** | The COM port where that adapter is right now, or **NOT DETECTED**. |

3. **Unplug all the jig's USB adapters** from the PC except the one for **Slot 1**.
4. Press **Rescan ports**. The **Detected interface** list now shows that one adapter, for example `COM4 - USB Serial Port - …`.
5. Set **Slot** to `1` and choose that adapter in **Detected interface**.
6. Press **Bind**. Type a reason when asked (for example `Initial setup` or `Replaced cable on slot 3`) and confirm.

   The table now shows Slot 1 with its USB identity and COM port.
7. Plug in the adapter for **Slot 2** (leave Slot 1 plugged in). Press **Rescan ports**.
8. In **Detected interface**, choose the adapter **without** a `[slot 1]` tag after it (`[slot N]` marks adapters already bound). Set **Slot** to `2` and press **Bind**.
9. Repeat for every slot (up to 4 or 8).
10. **Check:** every slot shows a USB identity, and **Currently detected as** shows a COM port, not NOT DETECTED.

**Recommended check:** load one module in each slot. In **Production settings → Enabled jig slots**, check that each slot shows the expected COM port. Then run one test batch and confirm that the module in each slot gives its result on the matching slot on screen.

**To remove a binding:** set **Slot** to that number, press **Unbind slot**, and give a reason.

**If an adapter is already bound to another slot,** the station refuses to bind it twice. First unbind the old slot, then bind the correct one.

> **Do not bind while modules are being programmed.** If you change bindings while a session is open, the Session page asks for **Re-confirm changed configuration** ([C6](#c6-start-of-every-shift--open-the-session-supervisor)).

## C4. Bind the Golden Rig — first time (Supervisor)

**When:** once per station, after the technician has finished [Part A](#part-a--one-time-certificate-setup-technician) and [B1](#b1-first-time-setup-of-the-rig-technician). The station will not run RF tests or open a session until it is bound to an approved rig.

**You need:** the rig's **name** (e.g. `golden-rig-01.local`) or IP, its **port** (normally `8762`), and the **fingerprint** the technician wrote down ([A2](#a2-create-the-certificates), step 5).

**Steps**

1. Log in as Supervisor. Open **Station configuration → Golden Rig binding**.

   At the top, **Approved binding** shows **NOT BOUND** in red.
2. **Find the rig.** Do one of the following:
   - **(a) Discover:** press **Discover rigs**. The table lists the rigs the technician configured, with *Address*, *Rig ID*, *In inventory*, *Certificate* and *Result*. Click the row of your rig: its name and port are filled in for you.
   - **(b) Type it:** in **mDNS name or IP**, type the rig's name, e.g. `golden-rig-01.local`. You can type an IP such as `192.168.10.50` instead, but the **name is recommended** because it keeps working when the rig's IP changes. Check **Port** (normally `8762`).

   Type only the name or IP: no `http://`, no `:8762` (the port has its own box) and no spaces.
3. Press **Request identity**. The screen shows *"Contacting Golden Rig..."*. This takes a few seconds.
4. **Read the result** (green text = good, red = cannot be approved):

   > **GOLDEN-RIG-01** at golden-rig-01.local:8762 (192.168.10.50) - certificate trusted by station CA (Golden Rig CA), valid until …, in approved inventory.
   > SHA-256: 4f1c…

   - The **rig ID** must be the one you expect.
   - The address in brackets is the IP the name was found at. It is for information only.
   - *"in approved inventory"* must be shown. If it says **NOT in approved inventory**, the **Approve** button stays disabled. Ask the technician to add the rig ID to `GOLDEN_RIG_INVENTORY`.
5. If you see an error instead, see [D. Problems](#d-problems-and-what-to-do) (for example *Cannot resolve …*, *Cannot reach …*, *not trusted by this station*).
6. **Compare the fingerprint.** The **SHA-256** on screen must match the fingerprint the technician gave you. Checking the first and last 8 characters is enough. **If they differ, do not approve**: call the technician.
7. Press **Approve station-to-rig binding**. Confirm the question *"Bind this station to Golden Rig …?"* with **Yes**, then type a reason (e.g. `Initial binding to GOLDEN-RIG-01`).

   The station contacts the rig once more, to make sure it is still the same rig, and saves the binding.
8. **Approved binding** now shows the **Rig ID** in green, the **Address**, the **Certificate SHA-256**, and who approved it and when. The approval also appears in **Binding audit history**.
9. **Repeat steps 1–8 on every station** that uses this rig. Each station is bound separately.

## C5. Production settings (Supervisor)

Open the **Production settings** tab. These settings are kept until a Supervisor changes them.

1. **Programming mode:** **Stock only**, or **Stock + Pricol**.
2. **Firmware images:** choose the **Stock firmware** file (and the **Pricol firmware** file for Stock + Pricol) with **Browse...**.
3. **Lot:** the lot or batch reference recorded with every batch.
4. **Enabled jig slots:** tick the slots that may be used. Each one shows its COM port. Untick a slot whose fixture is damaged; Operators will then not be able to use it.
5. Press **Save settings** (or **Discard changes**).

## C6. Start of every shift — open the session (Supervisor)

Operators can run batches only while a production session is open. Open one at the start of every shift.

1. Log in as Supervisor. Open **Station configuration → Session**. It shows **"No production session is open"** in red.
2. Press **Review configuration**. The table shows:
   - **Jig** and its number of slots.
   - **Slot 1…N:** the bound adapter, and under *Detected now*: **yes**, **NOT DETECTED**, or **unbound**.
   - **Golden Rig:** the approved rig ID, or **NOT BOUND**.
   - **Rig certificate:** the start of the approved fingerprint.
3. Check:
   - Every slot you will use says **yes**. If not, fix it with [C3](#c3-bind-jig-slots-to-com-ports-supervisor).
   - The Golden Rig is the expected one. If it says NOT BOUND, do [C4](#c4-bind-the-golden-rig--first-time-supervisor).
4. Tick both boxes:
   - **"I have identified the jig and confirm the COM-port-to-slot bindings"**
   - **"I confirm the approved Golden Rig binding"**
5. Press **Start production session**.
   - The station contacts the Golden Rig and checks that it shows **exactly** the approved certificate.
   - Success: *"Production session started. Operators may now log in."* and **Session OPEN since …** in green.
   - If it fails, the reason is shown in red (see [D. Problems](#d-problems-and-what-to-do)).
6. Hand over to the Operator. Press **Log out**; the Operator then logs in on the login screen.

**If something changes during the shift** (a slot binding or the rig binding), the Session page shows the problem in red. Once you have checked it, press **Re-confirm changed configuration** and give a reason.

## C7. Normal production — running a batch (Operator)

The Operator screen shows a large coloured **headline**, an **instruction** under it, a picture of the jig slots, and one large **button**. **Always do what the headline and instruction say.** The steps below are what you will normally see.

### Getting ready

1. Log in ([C2](#c2-logging-in)).
2. If the headline says **CHECK STATION**, press **Check station / refill MAC cache**. The station checks the jig, the Golden Rig and the MAC addresses.
   - **JIG READY - LOAD MODULES** (blue): continue.
   - **JIG NOT READY** or **STATION NOT READY** (red): call the Supervisor. The reason is shown in brackets.
3. **Load modules** into the jig. You can load only some slots: empty slots are simply skipped.
4. **Tap each slot on screen that holds a module.** The headline becomes **JIG READY - PRESS START** (green) and lists the loaded slots.
5. Press **START**.

### Stock programming and RF test (every batch)

| Headline | What you do |
|---|---|
| **PREPARE FOR STOCK PROGRAMMING** / **STOCK PROGRAM MODE** | Set the jig to *program* mode as shown, then press **CONTINUE**. |
| **PROGRAMMING MODULES** | Wait. Each module's chip is fully erased first, then the Stock firmware is flashed. **Do not remove modules.** |
| **VERIFYING STOCK MACS** | Wait. **Do not remove modules.** |
| **RESET MODULES FOR RF TEST** | The modules are still in boot mode after programming. Switch **every** module to **application mode** and **reset** it, then press **RUN RF TEST**. The RF test does not start until you press it. A module that was not reset will fail the RF test. |
| **TESTING MODULES** | Wait. The Golden Rig is checking each module. |
| **RF TEST COMPLETE** | Wait. |

### Pricol programming and functional test (only in Stock + Pricol mode)

Each of these steps needs you to switch or reset the modules by hand. Nothing starts until you press the button.

| Headline | What you do |
|---|---|
| **SWITCH MODULES TO BOOT MODE** | After the RF test the modules are running the application. Switch **every** module back to **boot (download) mode** and **reset** it, then press **PROGRAM PRICOL FIRMWARE**. |
| **FINAL PROGRAMMING**, **VERIFYING MAC ADDRESSES** | Wait. Each chip is fully erased again, then the Pricol firmware and Pricol MAC are written and read back. **Do not remove modules.** |
| **SWITCH MODULES TO APPLICATION MODE** | Switch **every** module from boot mode to **application mode**, then press **RUN APP TEST**. **Do not reset yet.** |
| **RESET ALL MODULES NOW** | Now **reset / power-cycle** every module (within 30 seconds). Each module prints a start-up message; the station waits for it and then sends *PRICOLAPP*. |
| **READY FOR DFU TEST** | Press **RUN DFU TEST**. **Do not reset yet.** |
| **RESET ALL MODULES NOW** | **Reset** every module again. The station waits for each start-up message, then sends *PRICOLDFU*. |
| **TESTS COMPLETE** | Wait. |

> **Order matters.** For **PROGRAM PRICOL FIRMWARE**: switch and reset first, then press. For **RUN APP TEST** and **RUN DFU TEST**: press first, then reset when the screen says **RESET ALL MODULES NOW**. The module accepts the test command only right after it starts. A module reset too early, or not at all, fails with *"no start-up message"* and cannot be retested in the same batch.

### QR scan and finish

| Headline | What you do |
|---|---|
| **SCAN MODULE QR** | Scan the QR label of the module in the slot named in the instruction (e.g. *"SLOT 3"*). **Every module is scanned, including failed ones.** For a failed module the instruction says *"Scan the QR on the FAILED module in SLOT n"*: scan it, then keep it apart from the good modules. The station moves on to the next slot by itself. This applies even if every module in the batch failed. |
| **FINAL VALIDATION** | Check the slot results on screen, then press **COMPLETE BATCH**. |
| **BATCH COMPLETE** | Remove the modules, then press **PREPARE NEXT BATCH**. Keep **passed** and **failed** modules apart, as the slot colours show. |

**About QR scanning**
- Scan **only the module in the slot that is asked for.**
- If you scan the same label twice by mistake, the second read is ignored. You do not need to do anything.
- If the station says **Duplicate QR: … is already bound at …**, the label is a duplicate somewhere on the line. Do not force it: call the Supervisor.

**Failed modules:** a failed slot is shown in red with the step that failed (and, for programming errors, an **ERR** number). The other slots carry on normally. Press **Error codes** in the top bar to see what each ERR number means. Put failed modules in the reject tray, or follow your Supervisor's instructions.

**If the headline says SUPERVISOR REQUIRED** (or anything you do not understand): stop and call the Supervisor. Do not remove modules unless told to.

## C8. Golden Rig — after the first time (Supervisor)

### C8.1 Normal days: nothing to bind

Once a station is bound, it stays bound. You do **not** bind the rig again at each shift. When you start the session ([C6](#c6-start-of-every-shift--open-the-session-supervisor)), the station checks the rig automatically. Every RF test checks it again.

### C8.2 Quick health check of the rig

1. Open **Station configuration → Golden Rig binding**.
2. Press **Discover rigs** (the bound rig is always included), **or** type the bound name in **mDNS name or IP** and press **Request identity**.
3. Result **trusted**, with the same rig ID and SHA-256 as under **Approved binding**: the rig is fine. Do **not** press Approve; nothing needs changing.

### C8.3 The rig's address or certificate changed

Use the table in [B3](#b3-changes-to-the-rig-that-need-re-approval) to decide what is needed. Then, on **each** station:

1. Open **Station configuration → Golden Rig binding**.
2. Enter the rig's name (recommended) or its new IP and port. Press **Request identity**.
3. Check the rig ID and compare the **new** fingerprint with the one the technician gave you.
4. Press **Approve station-to-rig binding**, answer **Yes**, and give a reason (e.g. `Rig certificate renewed`).
5. If a session is open, the station adopts the new binding with an audit record. Otherwise, start the session as usual ([C6](#c6-start-of-every-shift--open-the-session-supervisor)).

### C8.4 Taking a rig out of use

1. Open **Station configuration → Golden Rig binding**.
2. Press **Disable binding** and give a reason (e.g. `Rig sent for repair`).
3. The station shows *"Stock RF testing is blocked until a rig is bound."* No batches can pass the RF test until a rig is approved again ([C4](#c4-bind-the-golden-rig--first-time-supervisor)).

## C9. End of shift (Supervisor)

1. Let the running batch finish (**BATCH COMPLETE**). The Operator presses **Log out**.
2. Log in as Supervisor. Open **Station configuration → Session**. Press **Close session** and give a reason (e.g. `Shift end`). Confirm with **Yes**.
3. The station sends all records to the LAN server first.
   - If the server cannot be reached, you are asked whether to close anyway. **Yes** is safe: records stay on the station and are uploaded automatically later.
4. You can check uploads at any time:
   - The top bar shows **Server:** with the number of modules waiting.
   - **Station configuration → Server sync** has the details and a **Sync now** button.

---

## D. Problems and what to do

### Golden Rig binding and session start

| Message on screen | Likely cause | What to do |
|---|---|---|
| *Cannot resolve golden-rig-01.local: no answer from the system resolver or mDNS …* | The rig is off, on another network, or mDNS is blocked. | Check the rig is on and its cable is connected. Run `ping golden-rig-01.local` from the station. If ping fails but the rig is on, ask the technician to open UDP 5353 on the rig and station firewalls ([B1](#b1-first-time-setup-of-the-rig-technician), step 6), or bind by IP. |
| *Cannot reach golden-rig-01.local:8762: …* (timed out / refused) | The rig PC is on but the rig application is not running, the port is wrong, or TCP 8762 is blocked. | Restart the rig PC (the service starts by itself). Check the port is 8762. Ask the technician to check the firewall. |
| *… presented a certificate that is not trusted by this station …* | (1) The name or IP you typed is not in the rig's certificate, **or** (2) the rig's certificate was not made with your CA, **or** (3) the station is missing the right `golden_rig_ca.pem`. | (1) Use the name that is in the certificate (e.g. `golden-rig-01.local`), or ask the technician to add the IP. (2)/(3) Call the technician. |
| *Golden Rig CA certificate not found: …* | `golden_rig_ca.pem` is missing on this station. | Technician: [C1](#c1-first-time-station-setup-technician), step 2. |
| *This rig cannot be approved* / *NOT in approved inventory* | The rig ID is not in the station's approved list. | Technician adds the rig ID to `GOLDEN_RIG_INVENTORY`. |
| *The rig's identity changed since it was requested; request it again* | The rig changed between *Request identity* and *Approve*. | Press **Request identity** again, check, approve. If it keeps happening, call the technician. |
| *Rig at … presents a different certificate than the approved one* | The rig's certificate was renewed or replaced, **or** a different machine is answering at that address. | Get the new fingerprint from the technician. If it matches, re-approve ([C8.3](#c83-the-rigs-address-or-certificate-changed)). **If no one renewed it, do not approve: call the technician.** |
| *Rig ID … does not match approved …* | Another rig is answering at the bound address. | Check which rig should be used. Re-bind only after checking with the technician. |
| *No enabled Golden Rig binding* | The station has never been bound, or the binding was disabled. | [C4](#c4-bind-the-golden-rig--first-time-supervisor). |
| *… enter the port separately* / *… no http://, paths or spaces* / *… is not a valid host name* | The name was typed wrongly. | Type only the name or IP, e.g. `golden-rig-01.local`. Put the port in the **Port** box. |

### Jig and slots

| Message on screen | Likely cause | What to do |
|---|---|---|
| Slot shows **NOT DETECTED** | The adapter is unplugged, faulty, or moved to another USB socket. | Re-plug it into the same socket and press **Rescan ports**. If it was replaced or moved, re-bind the slot ([C3](#c3-bind-jig-slots-to-com-ports-supervisor)). |
| **JIG NOT READY** (Operator screen) | A slot or the jig is not detected. | Supervisor checks [C3](#c3-bind-jig-slots-to-com-ports-supervisor) and [C6](#c6-start-of-every-shift--open-the-session-supervisor). |
| An adapter does not appear in **Detected interface** | It has no USB identity the station can use (some cheap adapters), or its driver is not installed. | Try another adapter. Ask the technician to check Windows Device Manager. |
| Slot fails with **ERR: 6** (or another number) | Programming error from the module. 6 = the module did not answer (not in program mode, poorly seated, or no power). | Press **Error codes** for the meaning. Re-seat the module and check the jig mode switch. If one slot keeps failing, call the Supervisor. |
| Every module fails Pricol programming or the RF test at once | The button was pressed before the modules were switched: Pricol programming needs **boot mode**, the RF test needs **application mode** after a reset. | Switch and reset as the screen says **before** pressing the button. The affected modules cannot be retested in the same batch; set them aside for the Supervisor. |
| APP / DFU test fails with *"no start-up message"* | The modules were reset **before** RUN APP TEST / RUN DFU TEST was pressed, not reset at all, or are still in boot mode. | Put the modules in application mode, press the button **first**, then reset them when the screen says **RESET ALL MODULES NOW**. |

### Login

| Message on screen | What to do |
|---|---|
| Wrong user ID or password | Try again carefully. After 5 failures, wait 5 minutes. |
| Login server not reachable | Check the network cable. Call the Supervisor or IT: logins are checked by the LAN server. |
| Badge not recognised | Your badge may not be enrolled. Log in with your user ID, and ask the Supervisor to have the badge enrolled on the server. |

---

## E. Checklists

### First-time line setup (in this order)

| # | Who | Task | Section |
|---|---|---|---|
| 1 | 🟠 | Create the CA (once) and the rig certificate (per rig); note fingerprint, rig ID, rig name | [A2](#a2-create-the-certificates) |
| 2 | 🟠 | Rename the rig PC, install the certificates, start the rig service, open the firewall | [B1](#b1-first-time-setup-of-the-rig-technician) |
| 3 | 🟠 | Install StationApp, copy `golden_rig_ca.pem`, check `.env`; enrol users on the LAN server | [C1](#c1-first-time-station-setup-technician) |
| 4 | 🔵 | Bind every jig slot to its adapter | [C3](#c3-bind-jig-slots-to-com-ports-supervisor) |
| 5 | 🔵 | Bind and approve the Golden Rig (compare the fingerprint) | [C4](#c4-bind-the-golden-rig--first-time-supervisor) |
| 6 | 🔵 | Set programming mode, firmware, lot, enabled slots | [C5](#c5-production-settings-supervisor) |
| 7 | 🔵 | Open the production session | [C6](#c6-start-of-every-shift--open-the-session-supervisor) |
| 8 | 🟢 | Run a test batch; check each slot's result appears on the right slot | [C7](#c7-normal-production--running-a-batch-operator) |

### Every shift

| # | Who | Task |
|---|---|---|
| 1 | 🔵 | Golden Rig powered on and connected. |
| 2 | 🔵 | Log in → **Station configuration → Session** → **Review configuration** → tick both boxes → **Start production session**. |
| 3 | 🔵 | Check **Production settings** (mode, firmware, lot). Log out. |
| 4 | 🟢 | Log in → **Check station** → load modules → tap slots → **START** → follow the headline. |
| 5 | 🔵 | End of shift: **Close session** (records are sent to the server). |

### Needs a Supervisor (and maybe the technician)

- An adapter, cable or USB hub was replaced or moved → re-bind slots ([C3](#c3-bind-jig-slots-to-com-ports-supervisor)).
- The rig's certificate was renewed, or the rig was renamed or replaced → new certificate and re-approval on every station ([B3](#b3-changes-to-the-rig-that-need-re-approval), [C8.3](#c83-the-rigs-address-or-certificate-changed)).
- The rig was bound **by IP** and the IP changed → re-bind, preferably by name.
- A QR code is reported as already used, **SUPERVISOR REQUIRED** is shown, or a fingerprint does not match → stop and investigate.

---

## F. Bench tool: test the whole flow without the LAN server

🔵🟠 For Supervisors and engineers. The bench tool runs the same programming and test steps as production on one PC: Stock only, or Stock + Pricol. It needs **no LAN server, login or production session**. It uses its own **test MAC ranges** and writes nothing to the production database, so **never ship modules programmed on the bench as production modules.**

### F1. Start it

- Real jig: open a terminal in the StationApp folder and run `python -m stationapp.bench` (or `stationapp-bench`).
- No hardware: run `python -m stationapp.bench --simulate`. A brown **SIMULATION** banner is shown.

It reads the MPCLI path, firmware files, jig size, slot-to-COM bindings and QR scanner settings from the station `.env` when one is present.

### F2. Set it up (once; it is remembered)

1. **Programming:** choose **Stock only** or **Stock + Pricol**.
2. **MPCLI**, **Stock firmware** and, for Stock + Pricol, **Pricol firmware**: choose the files with **Browse...**.
3. **Test MAC ranges:** enter a first and last MAC for **Stock** and press **Set**. For Stock + Pricol, do the same for **Pricol**. The two ranges must not overlap. Each MAC is used once, even if programming fails. The line below each range shows the next MAC and how many are left.
4. **RF test:**
   - **Skip RF**: no RF test.
   - **Golden Rig**: only if this PC's station app has an approved rig. Use **Check connection** to test the rig first.
   - **Manual**: you check each module with a BLE phone app.
5. **QR code scanner:** tick **Scan module QRs after each run** to scan every module's QR at the end. **Scanner test** checks the scanner without programming anything.
6. In the table, tick **Use** for each slot that holds a module and check its **COM port**.

### F3. Run it

Press **START**. The steps follow, and a large blue instruction above the buttons always says what to do next. For each manual step: **first switch or reset the modules as the instruction says, then press the green button.**

| # | Instruction / button | What happens |
|---|---|---|
| 1 | *(automatic after START)* | Chip erased (per **Chip erase**, full by default), Stock firmware flashed, Stock MAC written and read back. |
| 2 | **RESET MODULES FOR RF TEST** → **RUN RF TEST** | Golden Rig only: switch the modules to application mode and reset them, then press. *(Manual RF opens a check list instead; Skip RF goes straight on.)* |
| 3 | **SWITCH MODULES TO BOOT MODE** → **PROGRAM PRICOL FIRMWARE** | Stock + Pricol only: switch the modules to boot mode and reset them, then press. Pricol firmware is flashed, and the Pricol MAC (from the Pricol range) written and read back. |
| 4 | **SWITCH MODULES TO APPLICATION MODE** → **RUN APP TEST** | Switch the modules to application mode, press, **then** reset (power-cycle) them when the orange *RESET … NOW* line appears. Each module's start-up message is awaited, then *PRICOLAPP* is sent on its COM port. |
| 5 | **READY FOR DFU TEST** → **RUN DFU TEST** | Press, **then** reset the modules again when told. *PRICOLDFU* is sent after each start-up message. |
| 6 | **SCAN QR of module in SLOT n** | If ticked: scan every module's QR, failed ones included. |

Only modules that passed a step go on to the next one. The others keep their result. While a run waits for you, **START** stays disabled.

**Stop early:** the grey button next to the green one (**Finish without RF**, **Stop here (no Pricol)**, **Stop here (no tests)**…) records the remaining steps as not done and finishes the run.

**Golden Rig not reachable:** the modules keep waiting. Fix the rig, then press **RUN RF TEST** again.

**Restart the workflow (repeat the test N times):** press the orange **RESTART WORKFLOW** button and confirm. It works at any point: while programming is running, while the run waits at a manual step, during QR scanning, or after a run is cancelled or finished.
- A step that is running is stopped first. MPCLI is given time to stop.
- The current run is recorded as it stands in the results file. Modules that had not finished are marked **ABORTED**.
- The complete workflow then starts again from step 1, with the slots and settings now selected. Each new run uses new test MACs.
- Before restarting, put the modules back in **boot (download) mode**, because the new run starts by flashing the Stock firmware.

### F4. Read the results

| Column | Meaning |
|---|---|
| **MAC / Readback / RF** | Stock MAC, its readback (MATCH or the MAC actually read), RF result. |
| **Pricol MAC / Pricol readback** | Stock + Pricol only. |
| **APP / DFU** | Functional test results, e.g. *APP PASS / DFU PASS*. Hover for the module's response. |
| **Result** | **PASS** / **PASS (no RF)** (green); **FAIL**, **RF FAIL**, **PRICOL FAIL**, **FUNCTIONAL FAIL** (red); pending or not run (amber). |
| **QR** | The scanned QR, or NOT SCANNED. |

- **Results folder:** **Open results folder** opens `logs\bench`, which holds `stock_bench_results.csv` (one row per module per run) and one folder of MPCLI logs per run.
- **MPCLI log:** double-click a row to open that module's log.
- **Error numbers:** press **Error codes** for what each **ERR** number means.
