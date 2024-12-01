{
  description = "Python development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    poetry2nix = {
      url = "github:nix-community/poetry2nix";
    };
    nixpkgs-docker-layering = {
      url = "github:colonelpanic8/nixpkgs/docker-customisable-layering-strategy";
    };
  };

  outputs = { self, nixpkgs, flake-utils, poetry2nix, nixpkgs-docker-layering }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        inherit (poetry2nix.lib.mkPoetry2Nix {inherit pkgs;}) mkPoetryApplication defaultPoetryOverrides;
        pkgs = nixpkgs.legacyPackages.${system};
        python-with-pip = pkgs.python312.withPackages (p:
          with p; [
            pip
          ]);
        dev-packages = with pkgs; [
          pyright
          black
          isort
          autoflake
          alejandra

          python-with-pip
          git
          poetry
          curl

          jq
          just
          kubernetes-helm
          kubectx
          kubectl
          terraform
          terragrunt
        ];
        other-packages = with pkgs; [
          boost
          glib
          stdenv.cc.cc
          zlib
          clang
        ];
        pypkgs-build-requirements = {
          asyncio = ["setuptools"];
        };
        p2n-overrides = defaultPoetryOverrides.extend (self: super:
        (builtins.mapAttrs (
            package: build-requirements:
              (builtins.getAttr package super).overridePythonAttrs (old: {
                buildInputs =
                  (old.buildInputs or [])
                  ++ (builtins.map (pkg:
                    if builtins.isString pkg
                    then builtins.getAttr pkg super
                    else pkg)
                  build-requirements);
              })
          )
          pypkgs-build-requirements));

      in {
        packages = rec {
          soad = mkPoetryApplication {
            projectDir = self;
            overrides = p2n-overrides;
            preferWheels = true;
            python = pkgs.python312;
            extras = [];
            groups = [];
            checkGroups = [];
          };
        };
        devShells.default = pkgs.mkShell rec {
          packages = [pkgs.poetry];
          nativeBuildInputs = dev-packages ++ other-packages;
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath nativeBuildInputs;
          STDENV = "${pkgs.stdenv.cc.cc.lib}";
          PYRIGHT = "${pkgs.pyright}/bin/pyright";
          shellHook =
          ''
            poetry env use ${python-with-pip}/bin/python
            export PATH="$PATH:$(pwd)/bin"
          ''
          + (
            if system == "x86_64-linux"
            then ''
              if [[ $(grep -i microsoft /proc/version) ]]; then
              export LD_LIBRARY_PATH=/usr/lib/wsl/lib:$LD_LIBRARY_PATH
              fi
            ''
            else ""
          );
        };
      });
}
