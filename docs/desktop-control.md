# The tray

Ninaivu sits in the system tray on Windows and the menu bar on a Mac:

```
.venv\Scripts\pythonw -m ninaivu.desktop.tray      # Windows
.venv/bin/python -m ninaivu.desktop.tray            # macOS, Linux
```

From a checkout it needs `requirements/requirements-desktop.txt` (pystray and
Pillow). The Windows and macOS installers (`installers/`) bring everything
and open it from the Start menu or Applications; **Start at sign-in** in its
own menu keeps it there.

One icon, one menu: whether Ninaivu is running; **Open the family app** and
**Open the console**; **Start**, **Stop** and **Restart**; **Check for an
update**; **View the log**; **Trust the HTTPS certificate**; **Start at
sign-in**; **Quit the tray**. Every action reports in a notification. Stop
asks the server to finish properly through its own authenticated shutdown
and never force-kills it; Quit leaves the server running, since the icon was
never what kept it up. The tray also finds and controls a server started
another way, such as `launcher/start.py` or a service.

Graphs, the live log pane and the resource mode moved to the console's
**Server** page, which the tray opens; the Tk window that showed them is
gone.

New desktop installations use HTTPS on port 443. The library address is **https://ninaivu.local** and the admin console is **https://ninaivu-admin.local**; neither needs a port number. Both names share the HTTPS listener, with the admin app's existing sign-in checks. A separately restricted admin bind remains restricted. Custom ports and disabled network discovery keep their explicit configuration. On the server computer, **https://localhost** is a library fallback if the network name does not resolve.

On a Mac, **Trust the HTTPS certificate** asks first, then trusts Ninaivu's certificate for websites in the login keychain. macOS asks for your password in its own window. The tray then checks that this Ninaivu's certificate verifies, and says so. It does not go through Keychain Access's import, which is where it went wrong: that offers the iCloud or Local Items keychain, which take no certificates, and refuses a certificate that is already there. Both say "unable to import", and neither marks it trusted. If trusting fails, the tray shows the file in Finder and gives the one step to do by hand: in Keychain Access, open "Ninaivu local CA" in the login keychain and set **When using this certificate** to **Always Trust**. If another certificate called "Ninaivu local CA" is in the keychains, from another Ninaivu or an earlier setup, the tray says so; it does nothing for this Ninaivu and can be deleted.

On Windows, **Trust the HTTPS certificate** adds the certificate to the current user's Trusted Root store after asking, and if that fails opens this installation's public CA certificate in the Windows certificate viewer. Trust it for your user account only if it is your own Ninaivu installation. Other devices must also trust this CA. Network access requires an appropriate private-network HTTPS firewall rule; the tray does not silently change Windows trust or firewall settings.
