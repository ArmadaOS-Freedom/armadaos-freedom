CC      := gcc
CFLAGS  := -O2 -Wall -Wextra -DHAVE_X11
LDFLAGS := $(shell pkg-config --cflags --libs x11 libdrm) -lm

TARGET  := build/armada-splash

PREFIX  ?= /usr
DESTDIR ?=

INSTALL         := install
INSTALL_DIR     := $(INSTALL) -dm755
INSTALL_PROGRAM := $(INSTALL) -Dm755
INSTALL_DATA    := $(INSTALL) -Dm644

.PHONY: all clean install uninstall

all: $(TARGET) build/font.ttf

# Deterministic single-face subset for the splash status text. The source
# collection is selected by family name inside the generator (matching
# load_font's face-selection), so the override only needs to name the TTC.
# Set SPLASH_FONT_SOURCE to a path to build against a non-system fixture.
SPLASH_FONT_SOURCE ?= $(firstword $(wildcard \
    /usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc \
    /usr/share/fonts/noto-sans-cjk/NotoSansCJK-Regular.ttc \
    /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc))
SPLASH_FONT_GEN    := scripts/gen-splash-font.py
SPLASH_FONT_CATALOGS := src/locales/en.h src/locales/zh-CN.h

build/font.ttf: $(SPLASH_FONT_GEN) $(SPLASH_FONT_CATALOGS)
	@mkdir -p build
	@test -n "$(SPLASH_FONT_SOURCE)" || { \
	    echo "ERROR: set SPLASH_FONT_SOURCE to a NotoSansCJK-Regular.ttc" >&2; exit 1; }
	python3 $(SPLASH_FONT_GEN) \
	    --source "$(SPLASH_FONT_SOURCE)" \
	    --output $@ \
	    $(foreach c,$(SPLASH_FONT_CATALOGS),--catalog $(c))

$(TARGET): src/armada-splash.c src/splash-i18n.c src/stb_impl.c \
           src/splash-i18n.h src/locales/en.h src/locales/zh-CN.h
	@mkdir -p build
	$(CC) -O2 -w -c src/stb_impl.c -o build/stb_impl.o
	$(CC) $(CFLAGS) -o $@ $< src/splash-i18n.c build/stb_impl.o $(LDFLAGS)
	strip $@
	rm -f build/stb_impl.o

clean:
	rm -rf build

install: all
	# Main binary
	$(INSTALL_DIR) $(DESTDIR)$(PREFIX)/libexec/armada/
	$(INSTALL_PROGRAM) $(TARGET) $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash

	# Dracut module
	$(INSTALL_DIR) $(DESTDIR)$(PREFIX)/lib/dracut/modules.d/91armada-splash
	$(INSTALL_DATA) system/usr/lib/dracut/modules.d/91armada-splash/armada-splash-initrd.service \
		$(DESTDIR)$(PREFIX)/lib/dracut/modules.d/91armada-splash/
	$(INSTALL_PROGRAM) system/usr/lib/dracut/modules.d/91armada-splash/module-setup.sh \
		$(DESTDIR)$(PREFIX)/lib/dracut/modules.d/91armada-splash/

	# systemd units
	$(INSTALL_DIR) $(DESTDIR)$(PREFIX)/lib/systemd/system
	$(INSTALL_DATA) system/usr/lib/systemd/system/armada-splash-early.service \
		$(DESTDIR)$(PREFIX)/lib/systemd/system/
	$(INSTALL_DATA) system/usr/lib/systemd/system/armada-splash-poweroff-screen.service \
		$(DESTDIR)$(PREFIX)/lib/systemd/system/
	$(INSTALL_DATA) system/usr/lib/systemd/system/armada-splash-reboot-screen.service \
		$(DESTDIR)$(PREFIX)/lib/systemd/system/
	$(INSTALL_DATA) system/usr/lib/systemd/system/armada-splash-stall.service \
		$(DESTDIR)$(PREFIX)/lib/systemd/system/

	# sddm drop-in
	$(INSTALL_DIR) $(DESTDIR)$(PREFIX)/lib/systemd/system/sddm.service.d
	$(INSTALL_DATA) system/usr/lib/systemd/system/sddm.service.d/armada-splash.conf \
		$(DESTDIR)$(PREFIX)/lib/systemd/system/sddm.service.d/

	# Helper scripts
	$(INSTALL_PROGRAM) system/usr/libexec/armada/armada-splash-launcher \
		$(DESTDIR)$(PREFIX)/libexec/armada/
	$(INSTALL_PROGRAM) system/usr/libexec/armada/armada-splash-progress \
		$(DESTDIR)$(PREFIX)/libexec/armada/
	$(INSTALL_PROGRAM) system/usr/libexec/armada/armada-splash-run \
		$(DESTDIR)$(PREFIX)/libexec/armada/
	$(INSTALL_PROGRAM) system/usr/libexec/armada/armada-splash-shutdown-msg \
		$(DESTDIR)$(PREFIX)/libexec/armada/
	$(INSTALL_PROGRAM) system/usr/libexec/armada/armada-splash-stall \
		$(DESTDIR)$(PREFIX)/libexec/armada/

	# Generated single-face subset for the status text. load_font() opens
	# this plain TTF; no code change is needed to consume it.
	$(INSTALL_DIR) $(DESTDIR)$(PREFIX)/share/armada/splash
	$(INSTALL_DATA) build/font.ttf \
		$(DESTDIR)$(PREFIX)/share/armada/splash/font.ttf

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/share/armada/splash/font.ttf
	rmdir $(DESTDIR)$(PREFIX)/share/armada/splash 2>/dev/null || true
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada-splash
	rm -rf $(DESTDIR)$(PREFIX)/lib/dracut/modules.d/91armada-splash
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/system/armada-splash-early.service
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/system/armada-splash-poweroff-screen.service
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/system/armada-splash-reboot-screen.service
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/system/armada-splash-stall.service
	rm -rf $(DESTDIR)$(PREFIX)/lib/systemd/system/sddm.service.d
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash-launcher
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash-progress
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash-run
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash-shutdown-msg
	rm -f $(DESTDIR)$(PREFIX)/libexec/armada/armada-splash-stall
