{
  description = "SaveSync Python Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

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
        wayland
        wayland-protocols
      ];

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
      ];

      savesyncPkg = pkgs.python312Packages.buildPythonApplication {
        pname = "savesync";
        version = "1.3.3";
        format = "other";

        src = ./.;

        nativeBuildInputs = [ pkgs.copyDesktopItems pkgs.makeWrapper ];

        propagatedBuildInputs = pythonDeps pkgs.python312Packages;

        installPhase = ''
          mkdir -p $out/share/savesync
          cp -r . $out/share/savesync

          mkdir -p $out/bin
          makeWrapper ${pkgs.python312.withPackages pythonDeps}/bin/python $out/bin/savesync \
            --add-flags "$out/share/savesync/main.py" \
            --prefix LD_LIBRARY_PATH : "${pkgs.lib.makeLibraryPath runtimeLibs}"
        '';
      };
    in
    {
      packages.${system} = {
        default = savesyncPkg;
        savesync = savesyncPkg;
      };

      devShells.${system}.default = pkgs.mkShell {
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
          xcbutilcursor
          wayland
        ];

        shellHook = ''
          export C_INCLUDE_PATH="${pkgs.linuxHeaders}/include:$C_INCLUDE_PATH"
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}:$LD_LIBRARY_PATH"
        '';
      };
    };
}