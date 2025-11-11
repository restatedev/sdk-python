{ pkgs ? import <nixpkgs> {} }:

with pkgs; mkShell {
	name = "sdk-python";
	buildInputs = [
			python3
			python3Packages.pip
			python3Packages.virtualenv
			uv
			just
			rustup
			cargo
			clang
			llvmPackages.bintools
			protobuf
			cmake
			pkg-config
	];

	RUSTC_VERSION =
		builtins.elemAt
		(builtins.match
		 ".*channel *= *\"([^\"]*)\".*"
		 (pkgs.lib.readFile ./rust-toolchain.toml)
		)
		0;

	LIBCLANG_PATH = pkgs.lib.makeLibraryPath [ pkgs.llvmPackages_latest.libclang.lib ];


	shellHook = ''
	  export UV_PYTHON=$(which python)
	'';

}
