# Build the prebuilt cc-switch-proxy binary from the patch.
#
#   make binary     apply the patch in a throwaway worktree, release-build it,
#                   strip it, and install it to bin/ (replacing the old binary)
#   make check      verify the patch applies cleanly to the checkout
#   make apply      apply the patch in place (for inspection; leaves the tree dirty)
#   make clean      remove the build worktree
#
# The cc-switch checkout defaults to ../cc-switch (the repo's submodule);
# override it when cc-switch is checked out somewhere else:
#
#   make binary CC_SWITCH_DIR=/path/to/cc-switch
#
# Requires a Rust toolchain and the Tauri Linux prerequisites (WebKitGTK/GTK dev
# packages) — see ../cc-switch/CONTRIBUTING.md. The cc-switch source tree is left
# untouched: the build runs in a detached git worktree outside the repo (default
# ~/.cache/cc-switch-proxy-build, override with BUILD_DIR=…).

CC_SWITCH_DIR ?= ../cc-switch
BUILD_DIR     ?= $(HOME)/.cache/cc-switch-proxy-build
PATCH         := $(abspath cc-switch-headless-proxy.patch)
OUT           := bin/cc-switch-proxy-linux-x86_64
BUILT_BIN     := $(BUILD_DIR)/src-tauri/target/release/cc-switch-proxy

.PHONY: binary check apply clean

binary: check
	@command -v cargo >/dev/null || { echo "error: cargo not found — install Rust (https://rustup.rs)"; exit 1; }
	@git -C "$(CC_SWITCH_DIR)" rev-parse --git-dir >/dev/null 2>&1 || { echo "error: $(CC_SWITCH_DIR) is not a git checkout (set CC_SWITCH_DIR)"; exit 1; }
	rm -rf "$(BUILD_DIR)"
	git -C "$(CC_SWITCH_DIR)" worktree add --detach "$(BUILD_DIR)" HEAD
	git -C "$(BUILD_DIR)" apply "$(PATCH)"
	mkdir -p "$(BUILD_DIR)/dist"
	cargo build --release --bin cc-switch-proxy --manifest-path "$(BUILD_DIR)/src-tauri/Cargo.toml"
	strip "$(BUILT_BIN)"
	mkdir -p bin
	install -m755 "$(BUILT_BIN)" "$(OUT)"
	git -C "$(CC_SWITCH_DIR)" worktree remove --force "$(BUILD_DIR)"
	@echo "installed $(OUT) (cc-switch $(CC_SWITCH_DIR) @ $$(git -C '$(CC_SWITCH_DIR)' rev-parse --short HEAD))"

check:
	@git -C "$(CC_SWITCH_DIR)" apply --check "$(PATCH)" \
		&& echo "patch applies cleanly to $(CC_SWITCH_DIR)"

apply: check
	git -C "$(CC_SWITCH_DIR)" apply "$(PATCH)"

clean:
	-git -C "$(CC_SWITCH_DIR)" worktree remove --force "$(BUILD_DIR)" 2>/dev/null
	rm -rf "$(BUILD_DIR)"
