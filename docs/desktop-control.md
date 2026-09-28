# Ninaivu Control Panel

**Until the installers in the roadmap exist,** the control panel is started
from the repository: `.venv\Scripts\python -m ninaivu.desktop.app` on Windows,
`.venv/bin/python -m ninaivu.desktop.app` on macOS and Linux. It is the same
panel on every system.

The native window starts independently of the server; no terminal is needed. It requires the existing `.venv` and `requirements-desktop.txt` dependencies. It is the same panel on both systems. A server it starts runs in the background, and on a Mac in a session of its own, so closing the panel leaves it running. It also finds and controls a server started another way, such as `start.py` or a service.

Use Start, Stop, Restart, Open Ninaivu, or Admin Console. Stop requests graceful shutdown using the local run-file token. It does not force-kill a busy server. Closing the panel leaves Ninaivu running. Server logs are available through View logs.

The redesigned panel includes a dark **Live logs** pane below the dashboard.
Drag the divider to resize it, choose **Server** or **Local AI**, pause updates,
or turn off auto-scroll to read earlier output. **Clear view** clears only the
display; it never deletes the log file. **View logs** focuses this embedded pane.
The reader loads at most 64 KB per update and displays at most 2,000 lines or
200 KB, keeping long-running logs manageable. Warnings and errors are colored.

New desktop installations use HTTPS on port 443. The library address is **https://ninaivu.local** and the admin console is **https://ninaivu-admin.local**; neither needs a port number. Both names share the HTTPS listener, with the admin app's existing sign-in checks. A separately restricted admin bind remains restricted. Custom ports and disabled network discovery keep their explicit configuration. On the server computer, **https://localhost** is a library fallback if the network name does not resolve.

On a Mac, **Install HTTPS certificate** asks first, then trusts Ninaivu's certificate for websites in the login keychain. macOS asks for your password in its own window. The panel then checks that this Ninaivu's certificate verifies, and says so. It does not go through Keychain Access's import, which is where it went wrong: that offers the iCloud or Local Items keychain, which take no certificates, and refuses a certificate that is already there. Both say "unable to import", and neither marks it trusted. If trusting fails, the panel shows the file in Finder and gives the one step to do by hand: in Keychain Access, open "Ninaivu local CA" in the login keychain and set **When using this certificate** to **Always Trust**. If another certificate called "Ninaivu local CA" is in the keychains, from another Ninaivu or an earlier setup, the panel says so; it does nothing for this Ninaivu and can be deleted.

On Windows, use **Install HTTPS certificate** to open this installation's public CA certificate in the Windows certificate viewer. Trust it for your user account only if it is your own Ninaivu installation. Other devices must also trust this CA. Network access requires an appropriate private-network HTTPS firewall rule; the panel does not silently change Windows trust or firewall settings.

During Stop, the panel shows **Stopping…** until the process exits, then **Stopped**. The shutdown request uses the configured HTTP or HTTPS transport. Logs are flushed immediately so startup failures are visible through View logs.

For explicitly approved Windows setup, `tools/enable-local-https.ps1` has three opt-in switches: `-TrustCertificate` installs this CA in the current user's trusted roots; `-AllowPrivateNetwork` allows TCP 443 and UDP 5353 from the local subnet on private profiles; `-RegisterLocalNames` adds the two default names pointing to loopback in this computer's hosts file. Firewall and hosts changes require administrator rights. Existing conflicting hosts entries are not overwritten. Trusting the CA allows this user to trust certificates signed by its private key; protect that key. These switches do nothing unless explicitly supplied.

Standard is the default. Performance allows more scanner workers, AI compute threads and request threads. Power-saving reduces concurrency and enables Windows execution-speed throttling for Ninaivu. Apply mode gracefully restarts a running server so the new limits take effect. Existing server arguments, including ports, are preserved. Archive ingestion keeps its existing safe concurrency limits. These modes do not change image resolution or the Windows system power plan; actual speed and battery benefits depend on workload and hardware.

The dashboard shows whole-computer CPU and memory, Ninaivu process CPU/memory/threads, battery level, disk space and CPU history. Battery power is the measured whole-device discharge rate, when the battery exposes an absolute wattage sensor. On AC power or unsupported hardware it shows unavailable; it does not invent per-app power estimates. Ollama is a separate local service; its usage contributes to computer totals.
