# Golden Rig Certificates — Create the Certificate, Key and Fingerprint

For the 🟠 **Technician**. This guide shows how to make the files that let every station connect **securely** to a Golden Rig, under the **rig ID and name you choose** for that rig.

Do it once for the line (the CA) and once for each rig (its certificate). Afterwards a Supervisor approves the rig on every station ([operator_supervisor_guide.md](operator_supervisor_guide.md), step C4).

---

## Contents

1. [What you will make](#1-what-you-will-make)
2. [Choose the rig ID and name](#2-choose-the-rig-id-and-name)
3. [Method 1 (recommended): Golden Rig certificate tool](#3-method-1-recommended-golden-rig-certificate-tool)
4. [Method 2: by hand with OpenSSL (Git Bash)](#4-method-2-by-hand-with-openssl-git-bash)
5. [Hand out the files](#5-hand-out-the-files)
6. [Install on the rig and approve on the stations](#6-install-on-the-rig-and-approve-on-the-stations)
7. [More rigs, renewal, replacement](#7-more-rigs-renewal-replacement)
8. [Problems](#8-problems)

---

## 1. What you will make

| File | What it is | Where it goes | Secret? |
|---|---|---|---|
| `golden_rig_ca.pem` | The line's **CA certificate**. Stations trust every rig signed by it. | **Every station:** `config\certificates\golden_rig_ca.pem` in the StationApp folder | No |
| `golden_rig_ca.key` | The CA's **master key**. Signs rig certificates. | **Nowhere.** Kept offline (encrypted USB stick, locked cabinet). | **Yes, most secret** |
| `rig.pem` | The rig's **certificate**: its rig ID and its network name(s). | That **Golden Rig** only | No |
| `rig.key` | The rig's **private key**. | That **Golden Rig** only | **Yes** |
| **Fingerprint** | 64-character SHA-256 of `rig.pem`. | **Supervisors**, on paper or by email | No |

How the station checks a rig when the Supervisor types its name:

1. The rig's certificate must be **signed by the CA** in the station's `golden_rig_ca.pem`.
2. The **name or IP the Supervisor types** must be written in the certificate. A rig made for `golden-rig-01.local` is refused at `192.168.10.50` unless that IP is in it too.
3. The certificate's **CN is the rig ID** that the station shows and records.
4. After approval, the station accepts only the certificate with **that exact fingerprint**.

The CA is made **once for the whole line** and reused for every rig. If your line already has `golden_rig_ca.pem` + `golden_rig_ca.key`, use them. A new CA means every station needs the new `golden_rig_ca.pem`.

## 2. Choose the rig ID and name

Decide these before you start, and write them down:

| Item | Example | Rule |
|---|---|---|
| **Rig ID** | `GOLDEN-RIG-01` | Unique per rig. Letters, digits, `-`, `_`, `.`. Stations show it to Supervisors; the rig service is started with exactly this ID. |
| **Rig name** | `golden-rig-01` | The rig PC's **computer name**. Letters, digits and `-` only (max 15 characters for Windows). Stations connect to `golden-rig-01.local`. **Recommended**: it keeps working when the rig's IP changes. |
| **Fixed IP** *(optional)* | `192.168.10.50` | Only if IT gives the rig a fixed IP **and** Supervisors may type the IP. Leave it out otherwise. |
| **Port** | `8762` | Normally unchanged. |

> The certificate contains the name in two forms, `golden-rig-01.local` and `golden-rig-01`, so Supervisors may type either. Anything not in the certificate (another name, an unlisted IP) is refused by the stations.

## 3. Method 1 (recommended): Golden Rig certificate tool

The tool is installed with Station App. It needs no Git, OpenSSL or Python, and it **tests the result with the same checks a station makes** before you hand anything out.

### 3.1 Start it

- On a PC with Station App: **Start menu → Station App → Golden Rig certificate tool**.
- Or run `GoldenRigCertTool.exe` in the StationApp folder (for example `C:\StationApp`).

Use a PC you trust. The CA key is created on it; you move it offline at the end.

### 3.2 Answer the questions

Press **Enter** to accept the value shown in `[brackets]`.

| Question | Type | Example |
|---|---|---|
| Rig ID (certificate CN) | your rig ID | `GOLDEN-RIG-01` |
| Rig PC name | the rig's computer name (default: made from the rig ID) | `golden-rig-01` |
| Fixed IP address(es) | the rig's fixed IP, or leave blank | `192.168.10.50` |
| Output folder | where the files are written | `C:\Users\<you>\Documents\GoldenRigCerts` |
| CA folder | the folder holding your line's **existing** `golden_rig_ca.pem` + `golden_rig_ca.key` (for example the USB stick). Leave the default for the **first** rig: a new CA is created there. | `E:\GoldenRigCA` |
| Also install the CA on this station? | `y` on a station PC, so it trusts the CA straight away (the old file is kept as `.bak`) | `y` |
| Create the certificate now? | check the summary, then `y` | `y` |

### 3.3 Read the result

```
Created a new CA:        C:\Users\tech\Documents\GoldenRigCerts\CA\golden_rig_ca.pem  (valid 10 years)
Station check OK:        golden-rig-01.local -> rig ID GOLDEN-RIG-01
Station check OK:        golden-rig-01 -> rig ID GOLDEN-RIG-01
Station check OK:        192.168.10.50 -> rig ID GOLDEN-RIG-01

Done. Files for GOLDEN-RIG-01: C:\Users\tech\Documents\GoldenRigCerts\GOLDEN-RIG-01
  SHA-256 fingerprint (as the station shows it): e434cb3aea6b1365...c9be70d9
  Valid until 2029-11-19.  Hand-out notes: ...\GOLDEN-RIG-01\RIG-INFO.txt
  Keep ...\CA\golden_rig_ca.key OFFLINE; it never goes on a station or a rig.
```

- Every name and IP must show **Station check OK**. If a check fails, nothing is ready to hand out; see [8. Problems](#8-problems).
- The rig's folder now holds `rig.pem`, `rig.key`, a copy of `golden_rig_ca.pem`, and **`RIG-INFO.txt`**. That file lists the rig ID, names, expiry date and **fingerprint**, and the exact commands for the rig PC and the values Supervisors type on the stations. Print it or email it (without `rig.key`).
- **Move the CA folder** (`golden_rig_ca.key`) to the offline USB stick and delete it from the PC.

### 3.4 Without questions (command line)

Open a Command Prompt in the StationApp folder:

```bat
GoldenRigCertTool.exe --rig-id GOLDEN-RIG-01 --name golden-rig-01 --ip 192.168.10.50 --ca-dir E:\GoldenRigCA --out D:\GoldenRigCerts
```

| Option | Meaning |
|---|---|
| `--rig-id` | Rig ID (required) |
| `--name` | Rig PC name (default: from the rig ID) |
| `--ip` | Fixed IP; repeat for more than one |
| `--ca-dir` | Folder with the existing CA (default `<out>\CA`, created if empty) |
| `--out` | Output folder (default `Documents\GoldenRigCerts`) |
| `--install-ca C:\StationApp` | Also install the CA on that station |
| `--force` | Replace an existing certificate for this rig ID |
| `--days 1095` | Validity of the rig certificate (never beyond the CA's expiry) |

In development: `python -m stationapp.tools.golden_rig_certs --help`.

## 4. Method 2: by hand with OpenSSL (Git Bash)

Use this only if the tool is not available. You need a Windows PC with **Git for Windows** (it includes `openssl`). Make an empty folder, e.g. `C:\rig-certs`, right-click inside it → **Open Git Bash here**, and paste the blocks one at a time.

### 4.1 The CA (once per line)

Skip this if your line already has a CA: copy its `golden_rig_ca.pem` and `golden_rig_ca.key` into the folder instead.

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

> Do **not** create the CA with `openssl req -x509 -addext …`. On some OpenSSL versions that makes a CA the station rejects with *"unable to get local issuer certificate"*.

### 4.2 Your rig's ID, name and IP

Change **only these three lines** to your values from [section 2](#2-choose-the-rig-id-and-name). Leave `RIG_IP=` empty if the rig has no fixed IP.

```bash
RIG_ID=GOLDEN-RIG-01
RIG_NAME=golden-rig-01
RIG_IP=192.168.10.50
```

### 4.3 The rig's key and certificate

Paste unchanged. It uses the values above and writes into a folder named after the rig ID.

```bash
SAN="DNS:${RIG_NAME}.local,DNS:${RIG_NAME}"
if [ -n "$RIG_IP" ]; then SAN="$SAN,IP:$RIG_IP"; fi
mkdir -p "$RIG_ID"

MSYS_NO_PATHCONV=1 openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout "$RIG_ID/rig.key" -out "$RIG_ID/rig.csr" -subj "/CN=$RIG_ID"

cat > "$RIG_ID/rig.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=$SAN
EOF

openssl x509 -req -in "$RIG_ID/rig.csr" -CA golden_rig_ca.pem -CAkey golden_rig_ca.key \
  -CAcreateserial -days 1095 -sha256 -extfile "$RIG_ID/rig.ext" -out "$RIG_ID/rig.pem"
cp golden_rig_ca.pem "$RIG_ID/"
```

### 4.4 Check it and get the fingerprint

```bash
openssl verify -CAfile golden_rig_ca.pem -verify_hostname "$RIG_NAME.local" "$RIG_ID/rig.pem"
openssl x509 -in "$RIG_ID/rig.pem" -noout -subject -ext subjectAltName -enddate
openssl x509 -in "$RIG_ID/rig.pem" -outform DER | openssl dgst -sha256
```

- The first line must print `GOLDEN-RIG-01/rig.pem: OK` (with your rig ID).
- The second shows `subject=CN = <rig ID>`, the names and IP, and the expiry date (`notAfter`). Put the expiry in the maintenance calendar.
- The third prints the **fingerprint exactly as the station shows it** (64 lowercase characters after `=`):
  ```
  (stdin)= e434cb3aea6b136544c7fd20f82ed74c8bd1f380590d5d7285b6c1bfc9be70d9
  ```
  Write it down for the Supervisors.

> `openssl x509 -fingerprint -sha256` prints the **same** value in another format (`E4:34:CB:…`, uppercase with colons). Give Supervisors the lowercase form from the third command; it is easier to compare with the screen.

`ca.csr`, `rig.csr`, `*.ext` and `*.srl` can be deleted afterwards.

## 5. Hand out the files

| File | Give to |
|---|---|
| `golden_rig_ca.pem` | **Every station**: copy to `config\certificates\golden_rig_ca.pem` in the StationApp folder, then restart Station App. (The installer installs the line's CA; replace it if yours is different.) |
| `rig.pem`, `rig.key` | **That rig only.** Copy them with a USB stick, not by email. |
| Rig ID, rig name, port, **fingerprint** (`RIG-INFO.txt`) | **Supervisors** |
| `golden_rig_ca.key` | **Nobody.** Keep it offline; you need it again only for the next rig or a renewal. |

## 6. Install on the rig and approve on the stations

**On the rig PC** (details: [operator_supervisor_guide.md](operator_supervisor_guide.md), B1):

1. Set the computer name to the **rig name**: *Settings → System → About → Rename this PC* → `golden-rig-01` → restart.
2. Copy `rig.pem` and `rig.key` to `C:\GoldenRig\certs\`. Make `rig.key` readable only by the rig service account.
3. Start the rig service with **exactly** the rig ID in the certificate:
   ```
   python golden_rig_server.py --rig-id GOLDEN-RIG-01 --cert C:\GoldenRig\certs\rig.pem --key C:\GoldenRig\certs\rig.key --port 8762
   ```
4. Firewall: TCP 8762 inbound; UDP 5353 (mDNS); network profile *Private*.
5. From a station PC: `ping golden-rig-01.local` must answer.

**On every station** (Supervisor, guide step C4): *Station configuration → Golden Rig binding* → type the rig name `golden-rig-01.local`, port `8762` → **Request identity**. The rig ID must be the one you chose, and the **SHA-256** must match the fingerprint (at least the first and last 8 characters). Then **Approve**.

## 7. More rigs, renewal, replacement

| Situation | What to do |
|---|---|
| **Another rig** | Same CA. Tool: give the CA folder of the line. OpenSSL: put `golden_rig_ca.pem` + `.key` in the folder, skip 4.1, change the three lines in 4.2. Stations already trust it; Supervisors approve the new rig. |
| **Rig certificate expires soon** | Make a new certificate with the **same** rig ID and name (tool: answer `y` to *Replace it*, or `--force`). Install it on the rig; Supervisors re-approve on every station with the **new** fingerprint. |
| **Rig renamed, or a new fixed IP must be typed** | New certificate with the new name / IP, same rig ID. Re-approve on every station. |
| **Rig PC replaced** | New certificate with a **new** rig ID (add it to `GOLDEN_RIG_INVENTORY` if used). Approve on every station. |
| **CA key lost or exposed** | New CA, new certificates for every rig, new `golden_rig_ca.pem` on every station, re-approval everywhere. Treat an exposed key as a security incident. |

## 8. Problems

| Message | Cause | Fix |
|---|---|---|
| Tool: *… exists but its key … is missing* | The CA folder has `golden_rig_ca.pem` without `golden_rig_ca.key`. | Give the folder that holds **both** files (the offline copy). |
| Tool: *… already exists. Replace it …?* | A certificate for this rig ID was made before. | Answer `y` only if you mean to renew or replace it; stations must then re-approve. |
| Tool: *Name …: use letters, digits and '-' only* | Spaces, `_` or `http://` in the name. | Type only the computer name, e.g. `golden-rig-01`. |
| Station: *… not trusted by this station: Hostname mismatch* | The name or IP typed on the station is not in the certificate. | Type the name from `RIG-INFO.txt`, or make a new certificate that includes that IP. |
| Station: *… not trusted … unable to get local issuer certificate* | The station's `golden_rig_ca.pem` is not the CA that signed the rig. | Copy the line's `golden_rig_ca.pem` to the station and restart it. |
| Station: *… NOT in approved inventory* | `GOLDEN_RIG_INVENTORY` in the station's `.env` does not list this rig ID. | Add the rig ID there (Start menu → Station App → Edit configuration), restart. |
| Fingerprint on screen differs from `RIG-INFO.txt` | A different or renewed certificate is on the rig, or another machine answers. | **Do not approve.** Check which `rig.pem` is installed on the rig. |
