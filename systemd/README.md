# Installing the timer on the Pi

Deploy the repo to `/opt/nsemom`, owned by a dedicated unprivileged user:

```bash
sudo useradd --system --home /opt/nsemom --shell /usr/sbin/nologin nsemom
sudo mkdir -p /opt/nsemom
sudo chown -R nsemom:nsemom /opt/nsemom

sudo -u nsemom git clone <repo> /opt/nsemom
cd /opt/nsemom
sudo -u nsemom python3 -m venv .venv
sudo -u nsemom ./.venv/bin/pip install -r requirements.txt
sudo -u nsemom ./.venv/bin/pip install -e .
```

Run the backfill once, in the foreground, before enabling the timer:

```bash
sudo -u nsemom ./.venv/bin/nsemom backfill
sudo -u nsemom ./.venv/bin/nsemom corpactions
sudo -u nsemom ./.venv/bin/nsemom adjust
sudo -u nsemom ./.venv/bin/nsemom indicators
```

Then install the units:

```bash
sudo cp systemd/nsemom.service systemd/nsemom.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nsemom.timer
```

## Checking it

```bash
systemctl list-timers nsemom.timer     # when it next fires, when it last ran
journalctl -u nsemom.service -n 100    # the last run's output
journalctl -u nsemom.service -f        # follow tonight's run
sudo systemctl start nsemom.service    # run it now, without waiting
```

## Verifying the schedule resolves as you expect

```bash
systemd-analyze calendar --iterations=5 'Mon..Fri 19:00 Asia/Kolkata'
```

If that errors, your systemd predates timezone suffixes (252). Drop the suffix
from `OnCalendar=` and set the machine timezone instead:

```bash
sudo timedatectl set-timezone Asia/Kolkata
```
