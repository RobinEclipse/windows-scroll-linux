#!/usr/bin/python3
"""Unprivileged on/off/status controls for desktop autoscroll."""
import sys,json
import dbus

def main():
 action=sys.argv[1] if len(sys.argv)>1 else 'toggle'
 if action not in ('enable','disable','toggle','status'):
  raise SystemExit('Usage: midscroll-control [enable|disable|toggle|status]')
 try:
  obj=dbus.SessionBus().get_object('org.midscroll.Control','/org/midscroll/Control')
  interface=dbus.Interface(obj,'org.midscroll.Control')
  if action=='status':print(json.dumps(json.loads(str(interface.Status())),indent=2))
  elif action=='toggle':print('on' if interface.Toggle() else 'paused')
  else:print('on' if interface.SetEnabled(dbus.Boolean(action=='enable')) else 'paused')
 except dbus.DBusException:
  raise SystemExit('Windows Scroll Linux v1 is not running. Start it with: systemctl --user start midscroll-overlay.service')
if __name__=='__main__':main()
