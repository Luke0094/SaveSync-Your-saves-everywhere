{
  description = "SaveSync Python Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      # Every Linux architecture nixpkgs builds for, rather than one.
      # x86_64 alone meant an aarch64 machine — a Raspberry Pi, an ARM
      # server, a Linux VM on Apple Silicon — got "does not provide
      # attribute packages.aarch64-linux.default" and no way in.
      #
      # Linux only, and deliberately: the runtime libraries below are X11,
      # XCB and Wayland. Claiming a darwin system would build a package
      # that cannot run.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems
          (system: f (import nixpkgs { inherit system; }));

      # Everything below is a function of pkgs now, so each system gets its
      # own set built from its own nixpkgs.
      perSystem = pkgs:
        let

        # Runtime shared libraries needed by PySide6 / Qt / X11 / Wayland.
        # Top-level pkgs is used directly since pkgs.xorg is deprecated in modern nixpkgs.
        runtimeLibs = with pkgs; [
          stdenv.cc.cc.lib
          zstd
          zlib
          libGL
          libGLU
          libxkbcommon
          krb5
          brotli
          glib
          libpng
          fontconfig
          freetype
          dbus
          wayland
          libX11
          libxcb
          libXext
          libXi
          libXrender
          libXrandr
          libXcursor
          libXinerama
          libXcomposite
          libXdamage
          libXfixes
          xcbutilcursor
          xcbutilimage
          xcbutilkeysyms
          xcbutilrenderutil
          xcbutilwm
        ];
        # wayland-protocols is XML/headers only — it ships no shared object, so
        # it belongs in a build input, never on LD_LIBRARY_PATH.

        pythonDeps = ps: with ps; [
          pyside6
          psutil
          watchdog
          requests
          google-auth
          google-auth-oauthlib
          google-api-python-client
          msal
          dropbox
          webdavclient3
          python-dateutil
          pynput
          cryptography
          keyring
          pillow
          langdetect
          pyyaml
          # Note: pillow-avif-plugin was removed in nixpkgs because
          # pillow >= 11.3 includes native AVIF support.
        ];

        savesyncPkg = pkgs.python312Packages.buildPythonApplication {
          pname = "savesync";
          version = "1.3.5";
          format = "other";

          # `./.` on its own drags offline_deps/, dist/ and build/ — about
          # 594 MB of build artefacts — into the store for a ~10 MB program,
          # and any of them changing forces a rebuild. Only the top-level
          # entries are matched, so core/build and core/dist are untouched.
          src = pkgs.lib.cleanSourceWith {
            src = ./.;
            filter = path: type:
              let
                rel = pkgs.lib.removePrefix (toString ./. + "/") (toString path);
              in
              !(builtins.elem rel [ "offline_deps" "dist" "build" ".git" ])
              && baseNameOf (toString path) != "__pycache__"
              && !(pkgs.lib.hasSuffix ".pyc" (toString path));
          };

          nativeBuildInputs = [ pkgs.copyDesktopItems pkgs.makeWrapper ];

          # copyDesktopItems was already in nativeBuildInputs with nothing for
          # it to copy — the hook ran and did nothing, which is the shape a
          # merge leaves when it keeps one half of a change.
          desktopItems = [
            (pkgs.makeDesktopItem {
              name = "savesync";
              exec = "savesync";
              icon = "savesync";
              comment = "SaveSync - Your saves everywhere";
              desktopName = "SaveSync";
              categories = [ "Utility" "Game" ];
            })
          ];

          propagatedBuildInputs = pythonDeps pkgs.python312Packages;

          installPhase = ''
            runHook preInstall

            mkdir -p $out/share/savesync
            cp -r . $out/share/savesync

            mkdir -p $out/share/icons/hicolor/256x256/apps
            cp assets/icon.png $out/share/icons/hicolor/256x256/apps/savesync.png

            mkdir -p $out/bin
            makeWrapper ${pkgs.python312.withPackages pythonDeps}/bin/python $out/bin/savesync \
              --add-flags "$out/share/savesync/main.py" \
              --prefix LD_LIBRARY_PATH : "${pkgs.lib.makeLibraryPath runtimeLibs}"

            runHook postInstall
          '';

          # $out/bin/savesync is a makeWrapper script around a python that
          # already carries the dependency set; buildPythonApplication's own
          # fixup would wrap it a second time.
          dontWrapPythonPrograms = true;

          # format = "other": there is nothing here for the Python import
          # check to import.
          doCheck = false;
        };
        in
        {
          package = savesyncPkg;
          devShell = pkgs.mkShell {
            packages = with pkgs; [
              python312
              python312Packages.pip
              python312Packages.setuptools
              python312Packages.wheel

              gcc
              linuxHeaders

              # Runtime dependencies for pip-installed PySide6/Qt
              krb5
              zstd
              zlib
              libGL
              libGLU
              libxkbcommon
              brotli
              glib
              libpng
              fontconfig
              freetype
              dbus
              wayland
              xcbutilcursor
            ];

            shellHook = ''
              export C_INCLUDE_PATH="${pkgs.linuxHeaders}/include:$C_INCLUDE_PATH"
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}:$LD_LIBRARY_PATH"
            '';
          };
        };
    in
    {
      packages = forAllSystems (pkgs:
        let built = perSystem pkgs; in {
          default = built.package;
          savesync = built.package;
        });

      devShells = forAllSystems (pkgs:
        { default = (perSystem pkgs).devShell; });
    };
}
