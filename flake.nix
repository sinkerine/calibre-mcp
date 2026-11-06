{
  description = "MCP server and direct HTTP download links for Calibre libraries";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      deps = ps: with ps; [
        mcp
        starlette
        uvicorn
      ];
    in
    {
      packages = forAllSystems (pkgs: rec {
        calibre-mcp = pkgs.python3Packages.buildPythonApplication {
          pname = "calibre-mcp";
          version = "0.1.0";
          pyproject = true;
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./pyproject.toml
              ./src
              ./tests
            ];
          };
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = deps pkgs.python3Packages;
          nativeCheckInputs = [ pkgs.python3Packages.unittestCheckHook ];
          unittestFlagsArray = [ "-s" "tests" ];
          # Put calibre's ebook-convert on PATH to offer EPUBs for books stored in other formats.
          meta = {
            description = "MCP server and direct HTTP download links for Calibre libraries";
            mainProgram = "calibre-mcp";
          };
        };
        default = calibre-mcp;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages deps) ];
          shellHook = ''export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"'';
        };
      });
    };
}
