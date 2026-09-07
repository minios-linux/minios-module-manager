PYTHON ?= python3
PREFIX ?= /usr
BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/lib/minios-module-manager
APPLICATIONSDIR = $(PREFIX)/share/applications
STYLEDIR = $(PREFIX)/share/minios-module-manager
MANDIR = $(PREFIX)/share/man/man1
MANROOT = $(PREFIX)/share/man
MANPAGE_LANGUAGES = de es fr id it pt pt_BR ru
LOCALEDIR = $(PREFIX)/share/locale
PO_FILES = $(wildcard po/*.po)

.PHONY: all test check install

all:

test:
	PYTHONPATH=lib $(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) -m py_compile \
		bin/minios-module-manager \
		lib/minios_module_manager/*.py \
		tests/*.py
	@for po in $(PO_FILES); do msgfmt --check --check-format -o /dev/null $$po; done
	$(MAKE) test

install:
	install -Dm755 bin/minios-module-manager $(DESTDIR)$(BINDIR)/minios-module-manager
	install -d $(DESTDIR)$(LIBDIR)/minios_module_manager
	install -m644 lib/minios_module_manager/*.py $(DESTDIR)$(LIBDIR)/minios_module_manager/
	install -Dm644 share/applications/minios-module-manager.desktop \
		$(DESTDIR)$(APPLICATIONSDIR)/minios-module-manager.desktop
	install -Dm644 share/style.css \
		$(DESTDIR)$(STYLEDIR)/style.css
	install -Dm644 manpages/en/minios-module-manager.1 \
		$(DESTDIR)$(MANDIR)/minios-module-manager.1
	@for lang in $(MANPAGE_LANGUAGES); do \
		install -Dm644 manpages/$$lang/minios-module-manager.$$lang.1 \
			$(DESTDIR)$(MANROOT)/$$lang/man1/minios-module-manager.1; \
	done
	@for po in $(PO_FILES); do \
		lang=$${po##*/}; lang=$${lang%.po}; \
		install -d $(DESTDIR)$(LOCALEDIR)/$$lang/LC_MESSAGES; \
		msgfmt -o $(DESTDIR)$(LOCALEDIR)/$$lang/LC_MESSAGES/minios-module-manager.mo $$po; \
	done
