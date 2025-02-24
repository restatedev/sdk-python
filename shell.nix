{ pkgs ? import <nixpkgs> {} }:

(pkgs.buildFHSEnv {
  name = "sdk-python";
  targetPkgs = pkgs: (with pkgs; [
    python3
    python3Packages.pip
    python3Packages.virtualenv
    just

		# rust
		rustup
		cargo
    clang
    llvmPackages.bintools
    protobuf
    cmake
		liburing
		pkg-config
  ]);

	RUSTC_VERSION =
	    builtins.elemAt
	    (builtins.match
	     ".*channel *= *\"([^\"]*)\".*"
	     (pkgs.lib.readFile ./rust-toolchain.toml)
	    )
	    0;

  LIBCLANG_PATH = pkgs.lib.makeLibraryPath [ pkgs.llvmPackages_latest.libclang.lib ];

  runScript = ''
		bash
  '';
}).env
