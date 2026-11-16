#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build LGPL FFmpeg shared libraries and programs for one kit.
#
# tools/qtci.py build-ffmpeg runs this script. It downloads and checks the
# sources, sets up the toolchain environment (vcvars64, MinGW, NDK), and
# passes everything in these environment variables:
#
#   FF_TARGET        msvc, mingw, linux, macos, or android
#   FF_SRC           extracted FFmpeg source directory
#   DAV1D_SRC        extracted dav1d source directory
#   NVCODEC_SRC      nv-codec-headers source (if the target uses it)
#   LIBVPL_SRC       libvpl source (if the target uses it)
#   AMF_SRC          AMF headers (if the target uses them)
#   ZLIB_SRC         zlib source (if the target uses it)
#   ARIBCAPTION_SRC  libaribcaption source (if the target uses it)
#   FF_WORK          empty scratch directory
#   FF_OUT           install directory
#   FF_CONFIGURE     FFmpeg configure options, one per line
#   FF_JOBS          parallel jobs
#   TOOLS_BIN        directory with meson, ninja, and cmake
#   ANDROID_NDK, ANDROID_ABI, ANDROID_API   android only
#
# On Windows, the script runs in MSYS2 bash. The paths in the variables use
# Windows form (C:/...).
#
# The configuration follows the FFmpeg that Qt builds for its own packages
# (coin/provisioning in qt5.git); versions/<qt_version>.json lists the
# changes. The extra parts are built as static libraries and linked into the
# FFmpeg libraries. No option may enable GPL or nonfree code: the script
# fails unless FFmpeg reports an LGPL license.

set -euo pipefail

for var in FF_TARGET FF_SRC DAV1D_SRC FF_WORK FF_OUT FF_CONFIGURE FF_JOBS TOOLS_BIN; do
    if [ -z "${!var:-}" ]; then
        echo "$var is not set" >&2
        exit 2
    fi
done

case "$FF_TARGET" in
    msvc|mingw) WINDOWS=1 ;;
    linux|macos|android) WINDOWS=0 ;;
    *) echo "unknown FF_TARGET: $FF_TARGET" >&2; exit 2 ;;
esac

# The script builds for the machine it runs on. The macos kit is arm64 only.
if [ "$FF_TARGET" = macos ] && [ "$(uname -m)" != arm64 ]; then
    echo "The macos kit needs an arm64 FFmpeg; this Mac is $(uname -m)" >&2
    exit 1
fi
if [ "$FF_TARGET" = macos ]; then
    # For every part (meson, CMake) and for FFmpeg, as in the FFmpeg of Qt.
    export MACOSX_DEPLOYMENT_TARGET=12
fi

# Path forms: meson, CMake, and the compilers get Windows paths (C:/...),
# the FFmpeg configure script and make get MSYS paths (/c/...).
unix_path() { if [ "$WINDOWS" = 1 ]; then cygpath -u "$1"; else printf '%s\n' "$1"; fi; }
mixed_path() { if [ "$WINDOWS" = 1 ]; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

SRC=$(unix_path "$FF_SRC")
WORK=$(unix_path "$FF_WORK")
OUT=$(unix_path "$FF_OUT")
DEPS="$WORK/deps"
DEPS_MIXED=$(mixed_path "$DEPS")
export PATH
PATH="$(unix_path "$TOOLS_BIN"):$PATH"

if [ "$FF_TARGET" = msvc ]; then
    # MSYS2 has its own link.exe (coreutils). The MSVC linker must come first.
    cl_path=$(command -v cl) || { echo "cl.exe is not on PATH (vcvars64)" >&2; exit 1; }
    PATH="$(dirname "$cl_path"):$PATH"
fi

mkdir -p "$WORK" "$DEPS"
rm -rf "$OUT"

if [ "$FF_TARGET" = android ]; then
    : "${ANDROID_NDK:?}" "${ANDROID_ABI:?}" "${ANDROID_API:?}"
    case "$ANDROID_ABI" in
        arm64-v8a) ARCH=aarch64; CPU=armv8-a; TRIPLE=aarch64-linux-android ;;
        x86_64)    ARCH=x86_64;  CPU=x86-64;  TRIPLE=x86_64-linux-android ;;
        *) echo "unsupported ABI: $ANDROID_ABI" >&2; exit 2 ;;
    esac
    TOOLCHAIN="$ANDROID_NDK/toolchains/llvm/prebuilt/linux-x86_64"
    if [ ! -x "$TOOLCHAIN/bin/clang" ]; then
        echo "NDK toolchain not found at $TOOLCHAIN" >&2
        exit 1
    fi
    CC_ANDROID="$TOOLCHAIN/bin/${TRIPLE}${ANDROID_API}-clang"
fi

# ------------------------------------------------------------------ dav1d
meson_args=(
    --buildtype=release
    --default-library=static
    --prefix="$DEPS_MIXED"
    --libdir=lib
    -Db_staticpic=true
    -Denable_tools=false
    -Denable_tests=false
    -Denable_examples=false
    -Denable_docs=false
)

if [ "$FF_TARGET" = msvc ]; then
    # The FFmpeg configure script compiles with the default C runtime of
    # cl.exe, the static one (/MT). Every part that is linked into the FFmpeg
    # libraries must use the same runtime.
    meson_args+=(-Db_vscrt=mt)
fi

if [ "$FF_TARGET" = android ]; then
    cat > "$WORK/android-cross.ini" <<EOF
[binaries]
c = '$CC_ANDROID'
ar = '$TOOLCHAIN/bin/llvm-ar'
strip = '$TOOLCHAIN/bin/llvm-strip'

[built-in options]
c_link_args = ['-Wl,-z,max-page-size=16384']

[host_machine]
system = 'android'
cpu_family = '$ARCH'
cpu = '$CPU'
endian = 'little'
EOF
    meson_args+=(--cross-file "$WORK/android-cross.ini")
fi

meson setup "${meson_args[@]}" "$(mixed_path "$WORK/dav1d-build")" "$DAV1D_SRC"
meson compile -C "$(mixed_path "$WORK/dav1d-build")"
meson install -C "$(mixed_path "$WORK/dav1d-build")"

if [ "$FF_TARGET" = msvc ]; then
    # meson names a static library libdav1d.a; the MSVC toolchain of the
    # FFmpeg configure script looks for dav1d.lib.
    mv "$DEPS/lib/libdav1d.a" "$DEPS/lib/dav1d.lib"
fi

# ---------------------------------------------------------- CMake parts
cmake_args=(
    -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_INSTALL_PREFIX="$DEPS_MIXED"
    -DCMAKE_INSTALL_LIBDIR=lib
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON
    -DBUILD_SHARED_LIBS=OFF
    # No download at configure time: libaribcaption falls back to fetching
    # FreeType when it cannot find it. That code would have no checksum and
    # no license file in the package.
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON
)
case "$FF_TARGET" in
    msvc)
        cmake_args+=(-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded
                     -DCMAKE_POLICY_DEFAULT_CMP0091=NEW) ;;
    macos)
        cmake_args+=(-DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=12) ;;
esac

cmake_part() {  # <name> <source> <extra cmake options...>
    local name="$1" src="$2"
    shift 2
    cmake "${cmake_args[@]}" "$@" -S "$src" -B "$(mixed_path "$WORK/$name-build")"
    cmake --build "$(mixed_path "$WORK/$name-build")" --parallel "$FF_JOBS"
    cmake --install "$(mixed_path "$WORK/$name-build")"
}

if [ -n "${ZLIB_SRC:-}" ]; then
    # zlib (Zlib license), for PNG, Matroska content compression, and more.
    # Qt gives its MSVC FFmpeg zlib too. Static only; the FFmpeg configure
    # script links -lz, which its MSVC toolchain reads as zlib.lib.
    cmake_part zlib "$ZLIB_SRC" -DZLIB_BUILD_SHARED=OFF -DZLIB_BUILD_TESTING=OFF
    if [ "$FF_TARGET" = msvc ]; then
        for lib in "$DEPS"/lib/zs.lib "$DEPS"/lib/zlibstatic.lib "$DEPS"/lib/zlibstatics.lib; do
            if [ -f "$lib" ]; then cp "$lib" "$DEPS/lib/zlib.lib"; break; fi
        done
        if [ ! -f "$DEPS/lib/zlib.lib" ]; then
            echo "The zlib build made no static library in $DEPS/lib:" >&2
            ls "$DEPS/lib" >&2
            exit 1
        fi
    fi
fi

if [ -n "${LIBVPL_SRC:-}" ]; then
    # The dispatcher of Intel VPL (MIT), for the QSV encoders.
    cmake_part libvpl "$LIBVPL_SRC" -DBUILD_TESTS=OFF -DBUILD_EXAMPLES=OFF \
        -DINSTALL_EXAMPLES=OFF -DUSE_MSVC_STATIC_RUNTIME=ON
    # vpl.pc has an empty Libs.private, but the static library needs the
    # registry and COM functions of Windows, and the C++ runtime with GCC.
    case "$FF_TARGET" in
        msvc)  vpl_private="-ladvapi32 -lole32" ;;
        mingw) vpl_private="-ladvapi32 -lole32 -lstdc++" ;;
        *)     vpl_private="-lstdc++ -ldl" ;;
    esac
    sed -i "s|^Libs.private:.*|Libs.private: $vpl_private|" "$DEPS/lib/pkgconfig/vpl.pc"
fi

if [ -n "${ARIBCAPTION_SRC:-}" ]; then
    # ARIB caption decoder (MIT). It renders with DirectWrite on Windows,
    # CoreText on macOS, and FreeType with Fontconfig of the system on Linux.
    cmake_part libaribcaption "$ARIBCAPTION_SRC" -DARIBCC_SHARED_LIBRARY=OFF \
        -DARIBCC_BUILD_TESTS=OFF
    if [ "$FF_TARGET" = mingw ]; then
        # CMake lists -liconv among the implicit libraries of MinGW g++. The
        # g++ specs also add it, so this does not stop the link of GNU
        # libiconv; it keeps the .pc file from asking for it. No FFmpeg code
        # uses iconv (it is off), so no libiconv code is linked today.
        sed -i 's/ -liconv\b//g' "$DEPS/lib/pkgconfig/libaribcaption.pc"
    fi
fi

# ------------------------------------------------- header-only parts
if [ -n "${NVCODEC_SRC:-}" ]; then
    # nv-codec-headers (MIT), for NVENC and NVDEC.
    make -C "$(unix_path "$NVCODEC_SRC")" install PREFIX="$DEPS_MIXED"
fi

if [ -n "${AMF_SRC:-}" ]; then
    # AMF headers (MIT), for the AMD encoders. They have no pkg-config file;
    # --extra-cflags below adds their directory.
    mkdir -p "$DEPS/include"
    cp -R "$(unix_path "$AMF_SRC")/AMF" "$DEPS/include/"
fi

# ----------------------------------------------------------------- FFmpeg
export PKG_CONFIG_PATH
PKG_CONFIG_PATH="$DEPS_MIXED/lib/pkgconfig"
if [ "$WINDOWS" = 1 ]; then
    # Windows pkg-config builds take ";" lists; MSYS2 pkgconf takes either.
    PKG_CONFIG_PATH="$DEPS/lib/pkgconfig"
fi
if [ "$FF_TARGET" = android ]; then
    # Cross build: the .pc files of the Linux host are not for Android.
    export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
fi

# A read loop, not mapfile: macOS has bash 3.2.
options=()
while IFS= read -r option; do
    options+=("$option")
done <<< "$FF_CONFIGURE"
args=(
    --prefix="$OUT"
    --enable-shared
    --disable-static
    --enable-libdav1d
    --pkg-config=pkg-config
    --pkg-config-flags=--static
    --extra-cflags="-I$DEPS_MIXED/include"
)
# The library directory of the parts (zlib has no pkg-config check).
if [ "$FF_TARGET" = msvc ]; then
    args+=(--extra-ldflags="-libpath:$DEPS_MIXED/lib")
else
    args+=(--extra-ldflags="-L$DEPS_MIXED/lib")
fi
for option in "${options[@]}"; do
    [ -n "$option" ] && args+=("$option")
done

case "$FF_TARGET" in
    msvc)
        args+=(--toolchain=msvc) ;;
    mingw)
        # The DLLs must need no MinGW runtime DLL, like the Qt kit (its
        # LDFLAGS have -static): libgcc, libstdc++ (libvpl, libaribcaption),
        # and libwinpthread (clock_gettime) are linked in, and Windows
        # threads replace pthreads.
        args+=(--target-os=mingw32 --arch=x86_64 --extra-ldflags=-static
               --disable-pthreads --enable-w32threads) ;;
    android)
        args+=(
            --enable-cross-compile
            --target-os=android
            --arch="$ARCH"
            --cpu="$CPU"
            --sysroot="$TOOLCHAIN/sysroot"
            --cc="$CC_ANDROID"
            --cxx="$TOOLCHAIN/bin/${TRIPLE}${ANDROID_API}-clang++"
            --ar="$TOOLCHAIN/bin/llvm-ar"
            --ranlib="$TOOLCHAIN/bin/llvm-ranlib"
            --nm="$TOOLCHAIN/bin/llvm-nm"
            --strip="$TOOLCHAIN/bin/llvm-strip"
            # 16 KB page alignment is required for apps that target Android 15.
            "--extra-ldflags=-Wl,-z,max-page-size=16384"
        ) ;;
esac

mkdir -p "$WORK/ffmpeg-build"
cd "$WORK/ffmpeg-build"
"$SRC/configure" "${args[@]}" || { tail -n 50 ffbuild/config.log >&2 || true; exit 1; }

# The license gate. FFmpeg writes the license that its configuration
# results in; an option that pulls in GPL or nonfree code changes it.
license_line=$(grep -E '^#define FFMPEG_LICENSE ' config.h) ||
    { echo "config.h has no FFMPEG_LICENSE line" >&2; exit 1; }
if [ "$license_line" != '#define FFMPEG_LICENSE "LGPL version 2.1 or later"' ]; then
    echo "FFmpeg is not LGPL 2.1 or later: $license_line" >&2
    exit 1
fi

make -j"$FF_JOBS"
make install

mkdir -p "$OUT/licenses"
grep -E '^#define FFMPEG_(LICENSE|CONFIGURATION) ' config.h > "$OUT/licenses/ffmpeg-config.txt"

# ----------------------------------------------------------- fix-ups
case "$FF_TARGET" in
    linux)
        # Each library finds the other FFmpeg libraries in its own directory,
        # and the programs find them in ../lib.
        for lib in "$OUT"/lib/lib*.so.*.*; do
            # shellcheck disable=SC2016
            patchelf --set-rpath '$ORIGIN' "$lib"
        done
        for program in "$OUT"/bin/*; do
            # shellcheck disable=SC2016
            patchelf --set-rpath '$ORIGIN/../lib' "$program"
        done ;;
    macos)
        # Same as fix_relative_dependencies.sh of Qt: install names
        # relative to @rpath, references to each other relative to the
        # library, and references of the programs relative to the program.
        for file in "$OUT"/lib/*.dylib "$OUT"/bin/*; do
            [ -L "$file" ] && continue
            case "$file" in
                *.dylib)
                    prefix="@loader_path"
                    id=$(otool -D "$file" | sed -n '2p')
                    case "$id" in
                        "$OUT"/lib/*) install_name_tool -id "@rpath/${id#"$OUT"/lib/}" "$file" ;;
                    esac ;;
                *) prefix="@executable_path/../lib" ;;
            esac
            otool -L "$file" | awk '/\t/ {print $1}' | while read -r dep; do
                case "$dep" in
                    "$OUT"/lib/*) install_name_tool -change "$dep" "$prefix/${dep#"$OUT"/lib/}" "$file" ;;
                esac
            done
        done ;;
esac

# The pkg-config files name the install directory of this build. Make them
# relative to their own location, so that they work where a user extracts
# the archive.
for pc in "$OUT"/lib/pkgconfig/*.pc; do
    # The library directory of the parts exists only during the build. Remove
    # each such option as a whole token: libvpl adds lib/pkgconfig/../ to it.
    sed -e "s|^prefix=.*|prefix=\${pcfiledir}/../..|" \
        -e "s|$OUT|\${prefix}|g" \
        -e "s| *-libpath:$DEPS_MIXED/lib[^ ]*||g" -e "s| *-L$DEPS_MIXED/lib[^ ]*||g" \
        -e "s| *-L$DEPS/lib[^ ]*||g" "$pc" > "$pc.tmp"
    mv "$pc.tmp" "$pc"
done

# The parts are inside the FFmpeg libraries; their files are not needed.
rm -rf "$OUT/share/ffmpeg" "$OUT/share/man"
echo "FFmpeg for $FF_TARGET installed in $OUT"
