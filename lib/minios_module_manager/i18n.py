"""gettext support for MiniOS Module Manager."""

import gettext
import os


DOMAIN = 'minios-module-manager'
LOCALE_DIR = os.environ.get(
    'MINIOS_MODULE_MANAGER_LOCALE_DIR', '/usr/share/locale')

gettext.bindtextdomain(DOMAIN, LOCALE_DIR)
gettext.textdomain(DOMAIN)
_translation = gettext.translation(
    DOMAIN, localedir=LOCALE_DIR, fallback=True)
_ = _translation.gettext
