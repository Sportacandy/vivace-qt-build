#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build FFmpeg shared libraries and programs for one kit: the LGPL FFmpeg of
# the kits, or the GPL FFmpeg of the Build FFmpeg workflow.
#
# tools/qtci.py build-ffmpeg runs this script. It downloads and checks the
# sources, sets up the toolchain environment (vcvars64, MinGW, NDK), and
# passes everything in these environment variables:
#
#   FF_TARGET        msvc, mingw, linux, macos, or android
#   FF_LICENSE       the license that FFmpeg must report, for example
#                    "LGPL version 2.1 or later"
#   FF_SRC           extracted FFmpeg source directory
#   DAV1D_SRC        extracted dav1d source directory
#   NVCODEC_SRC      nv-codec-headers source (if the target uses it)
#   LIBVPL_SRC       libvpl source (if the target uses it)
#   AMF_SRC          AMF headers (if the target uses them)
#   ZLIB_SRC         zlib source (if the target uses it)
#   ARIBCAPTION_SRC  libaribcaption source (if the target uses it)
#   X264_SRC, X265_SRC, ...   the parts of the GPL FFmpeg (see "GPL parts")
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
# FFmpeg libraries. No option may enable nonfree code, and the script fails
# unless FFmpeg reports the license in FF_LICENSE.

set -euo pipefail

for var in FF_TARGET FF_LICENSE FF_SRC DAV1D_SRC FF_WORK FF_OUT FF_CONFIGURE FF_JOBS TOOLS_BIN; do
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
        x86_64)    ARCH=x86_64;  CPU="x86-64";  TRIPLE=x86_64-linux-android ;;
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
cpp = '$TOOLCHAIN/bin/${TRIPLE}${ANDROID_API}-clang++'
ar = '$TOOLCHAIN/bin/llvm-ar'
pkg-config = 'pkg-config'
strip = '$TOOLCHAIN/bin/llvm-strip'

[built-in options]
c_link_args = ['-Wl,-z,max-page-size=16384']
cpp_link_args = ['-Wl,-z,max-page-size=16384']

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
    android)
        # The parts find each other in the install directory of the parts,
        # also through the NDK toolchain, which searches only its root paths.
        cmake_args+=(-DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK/build/cmake/android.toolchain.cmake"
                     -DANDROID_ABI="$ANDROID_ABI" -DANDROID_PLATFORM="android-$ANDROID_API"
                     -DCMAKE_FIND_ROOT_PATH="$DEPS_MIXED"
                     "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,-z,max-page-size=16384") ;;
esac
cmake_args+=(-DCMAKE_PREFIX_PATH="$DEPS_MIXED")

cmake_part() {  # <name> <source> <extra cmake options...>
    local name="$1" src="$2"
    shift 2
    cmake "${cmake_args[@]}" "$@" -S "$src" -B "$(mixed_path "$WORK/$name-build")"
    cmake --build "$(mixed_path "$WORK/$name-build")" --parallel "$FF_JOBS"
    cmake --install "$(mixed_path "$WORK/$name-build")"
}

if [ -n "${ZLIB_SRC:-}" ]; then
    # zlib (Zlib license), for PNG, Matroska content compression, and more.
    # Qt gives its MSVC FFmpeg zlib too, and the GPL MinGW FFmpeg builds it
    # instead of the zlib of MinGW, so that its source archive has the zlib
    # source. Static only; the FFmpeg configure script links -lz, which its
    # MSVC toolchain reads as zlib.lib, and MinGW as libz.a. On Windows, the
    # zlib CMake build names the static library zs.
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
    elif [ "$FF_TARGET" = mingw ]; then
        if [ ! -f "$DEPS/lib/libzs.a" ]; then
            echo "The zlib build made no libzs.a in $DEPS/lib:" >&2
            ls "$DEPS/lib" >&2
            exit 1
        fi
        cp "$DEPS/lib/libzs.a" "$DEPS/lib/libz.a"
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

# -------------------------------------------------------------- GPL parts
# The parts of the GPL FFmpeg (toolchains.ffmpeg.gpl). Each runs only when
# qtci.py passes its source; the LGPL FFmpeg of the kits passes none. All are
# static libraries that the FFmpeg libraries link. The order follows their
# dependencies: ogg before vorbis and theora, FreeType before HarfBuzz, the
# text parts before libass, and mbedtls before SRT.

# The C++ runtime of the C++ parts. FFmpeg links with the C compiler, so their
# .pc files must name it.
case "$FF_TARGET" in
    macos)   CXX_LIBS="-lc++" ;;
    android) CXX_LIBS="-lc++_static -lc++abi" ;;
    *)       CXX_LIBS="-lstdc++" ;;
esac

# Android parts that use autotools or their own configure scripts.
if [ "$FF_TARGET" = android ]; then
    android_env=(CC="$CC_ANDROID" CXX="$TOOLCHAIN/bin/${TRIPLE}${ANDROID_API}-clang++"
                 AR="$TOOLCHAIN/bin/llvm-ar" RANLIB="$TOOLCHAIN/bin/llvm-ranlib"
                 NM="$TOOLCHAIN/bin/llvm-nm" STRIP="$TOOLCHAIN/bin/llvm-strip")
fi

# Copies a source tree into the work directory, for the parts that build in
# their source tree or change it (autogen.sh).
copy_source() {  # <name> <source>
    rm -rf "${WORK:?}/$1-src"
    cp -R "$(unix_path "$2")" "$WORK/$1-src" || exit 1
    printf '%s\n' "$WORK/$1-src"
}

# Xvid 1.3.7 and LAME 3.100 have config.guess and config.sub files from before
# arm64 macOS. Newer copies come with automake, which zimg needs anyway.
refresh_config_scripts() {  # <directory>
    local libdir
    libdir=$(automake --print-libdir) || { echo "automake is not installed" >&2; exit 1; }
    for f in config.guess config.sub; do
        if [ -f "$1/$f" ]; then cp "$libdir/$f" "$1/$f"; fi
    done
}

# The --host (and --build) options of an autotools part. Android is a cross
# build. MinGW names its triplet, because config.guess in MSYS2 MINGW64
# reports x86_64-pc-mingw64, which the configure script of Xvid does not know
# (it matches mingw32*). Linux and macOS let config.guess find the host.
host_args=()
case "$FF_TARGET" in
    android) host_args=(--host="$TRIPLE") ;;
    mingw)   host_args=(--build=x86_64-w64-mingw32 --host=x86_64-w64-mingw32) ;;
esac

# An autotools part: a static library with position-independent code.
autotools_part() {  # <directory with configure> <extra configure options...>
    local dir="$1"
    shift
    local host=(${host_args[@]+"${host_args[@]}"})
    (
        cd "$dir"
        env ${android_env[@]+"${android_env[@]}"} ./configure --prefix="$DEPS_MIXED" \
            --enable-static --disable-shared --with-pic ${host[@]+"${host[@]}"} "$@"
        make -j"$FF_JOBS"
        make install
    )
}

# Adds libraries to the Libs.private line of a .pc file (the C++ runtime of a
# C++ part, or a system library that the part's .pc file leaves out).
add_private_libs() {  # <pc name> <libraries>
    local pc="$DEPS/lib/pkgconfig/$1.pc"
    [ -f "$pc" ] || { echo "$pc not found" >&2; exit 1; }
    if grep -q '^Libs.private:' "$pc"; then
        sed -i.bak "s|^Libs.private:\(.*\)|Libs.private:\1 $2|" "$pc"
    else
        printf 'Libs.private: %s\n' "$2" >> "$pc"
    fi
    rm -f "$pc.bak"
}

# meson options of a static part, as for dav1d.
meson_part() {  # <name> <source> <extra meson options...>
    local name="$1" src="$2"
    shift 2
    meson setup "${meson_args[@]}" "$@" "$(mixed_path "$WORK/$name-build")" "$src"
    meson compile -C "$(mixed_path "$WORK/$name-build")"
    meson install -C "$(mixed_path "$WORK/$name-build")"
}
# dav1d's options are not for the other meson parts.
meson_args=(
    --buildtype=release
    --default-library=static
    --prefix="$DEPS_MIXED"
    --libdir=lib
    -Db_staticpic=true
    --wrap-mode=nodownload
)
if [ "$FF_TARGET" = android ]; then
    meson_args+=(--cross-file "$WORK/android-cross.ini")
fi

x86=0
case "$FF_TARGET:${ARCH:-$(uname -m)}" in
    *:x86_64|*:AMD64) x86=1 ;;
esac

if [ -n "${X264_SRC:-}" ]; then
    # x264 (GPL 2 or later). All bit depths in one library.
    x264_args=(--prefix="$DEPS_MIXED" --enable-static --enable-pic --disable-cli
               --disable-lavf --disable-swscale --disable-ffms --disable-gpac
               --disable-lsmash --disable-opencl)
    if [ "$FF_TARGET" = android ]; then
        x264_args+=(--host="$TRIPLE" --cross-prefix="$TOOLCHAIN/bin/llvm-"
                    --sysroot="$TOOLCHAIN/sysroot")
    fi
    mkdir -p "$WORK/x264-build"
    (
        cd "$WORK/x264-build"
        env ${android_env[@]+"${android_env[@]}"} "$(unix_path "$X264_SRC")/configure" "${x264_args[@]}"
        make -j"$FF_JOBS"
        make install-lib-static
    )
fi

if [ -n "${X265_SRC:-}" ]; then
    # x265 (GPL 2 or later): the 8-bit library with the 10-bit and 12-bit
    # libraries linked in, as in the FFmpeg builds of BtbN. On AArch64, the
    # 10-bit and 12-bit libraries build without Neon code, neither assembly
    # nor intrinsics (x265 4.1 has it for them, which this build does not
    # use yet), and SVE and SVE2 are off at every bit depth. Android has no
    # SIMD code at any bit depth.
    x265_asm=ON
    x265_hbd_asm=ON
    if [ "$x86" = 0 ]; then x265_hbd_asm=OFF; fi
    if [ "$FF_TARGET" = android ]; then x265_asm=OFF; x265_hbd_asm=OFF; fi
    x265_common=(-DENABLE_SHARED=OFF -DENABLE_CLI=OFF -DENABLE_LIBNUMA=OFF
                 -DENABLE_PIC=ON -DENABLE_SVE=OFF -DENABLE_SVE2=OFF)
    x265_dir=$(mixed_path "$(unix_path "$X265_SRC")/source")
    for depth in 12 10; do
        extra=(-DHIGH_BIT_DEPTH=ON -DEXPORT_C_API=OFF "-DENABLE_ASSEMBLY=$x265_hbd_asm")
        if [ "$depth" = 12 ]; then extra+=(-DMAIN12=ON); fi
        cmake "${cmake_args[@]}" "${x265_common[@]}" "${extra[@]}" \
            -S "$x265_dir" -B "$(mixed_path "$WORK/x265-$depth")"
        cmake --build "$(mixed_path "$WORK/x265-$depth")" --parallel "$FF_JOBS"
    done
    mkdir -p "$WORK/x265-8"
    cp "$WORK/x265-10/libx265.a" "$WORK/x265-8/libx265_main10.a"
    cp "$WORK/x265-12/libx265.a" "$WORK/x265-8/libx265_main12.a"
    cmake "${cmake_args[@]}" "${x265_common[@]}" "-DENABLE_ASSEMBLY=$x265_asm" \
        -DEXTRA_LIB="x265_main10.a;x265_main12.a" -DEXTRA_LINK_FLAGS=-L. \
        -DLINKED_10BIT=ON -DLINKED_12BIT=ON \
        -S "$x265_dir" -B "$(mixed_path "$WORK/x265-8")"
    cmake --build "$(mixed_path "$WORK/x265-8")" --parallel "$FF_JOBS"
    cmake --install "$(mixed_path "$WORK/x265-8")"
    # One static library with all three bit depths.
    (
        cd "$WORK/x265-8"
        if [ "$FF_TARGET" = macos ]; then
            libtool -static -o libx265-all.a libx265.a libx265_main10.a libx265_main12.a
        else
            ar_tool="ar"
            if [ "$FF_TARGET" = android ]; then ar_tool="$TOOLCHAIN/bin/llvm-ar"; fi
            printf 'CREATE libx265-all.a\nADDLIB libx265.a\nADDLIB libx265_main10.a\nADDLIB libx265_main12.a\nSAVE\nEND\n' |
                "$ar_tool" -M
        fi
        cp libx265-all.a "$DEPS/lib/libx265.a"
    )
    # x265.pc names the C++ runtime of the compiler that built it, or none.
    sed -i.bak '/^Libs.private:/d' "$DEPS/lib/pkgconfig/x265.pc"
    rm -f "$DEPS/lib/pkgconfig/x265.pc.bak"
    case "$FF_TARGET" in
        linux)   add_private_libs x265 "$CXX_LIBS -lm -ldl -lpthread" ;;
        android) add_private_libs x265 "$CXX_LIBS -lm -ldl" ;;
        *)       add_private_libs x265 "$CXX_LIBS" ;;
    esac
fi

if [ -n "${LIBVPX_SRC:-}" ]; then
    # libvpx (BSD), VP8 and VP9. configure finds the target of a native build.
    vpx_args=(--prefix="$DEPS_MIXED" --enable-static --disable-shared --enable-pic
              --disable-examples --disable-tools --disable-docs --disable-unit-tests
              --enable-vp9-highbitdepth --disable-install-bins)
    if [ "$x86" = 1 ]; then vpx_args+=(--as=nasm); fi
    case "$FF_TARGET:${ANDROID_ABI:-}" in
        android:arm64-v8a) vpx_args+=(--target=arm64-android-gcc) ;;
        android:x86_64)    vpx_args+=(--target=x86_64-android-gcc --disable-realtime-only) ;;
        macos:*)           vpx_args+=(--target=arm64-darwin21-gcc) ;;
    esac
    mkdir -p "$WORK/libvpx-build"
    (
        cd "$WORK/libvpx-build"
        env ${android_env[@]+"${android_env[@]}"} ${android_env[@]+LD="$CC_ANDROID"} \
            "$(unix_path "$LIBVPX_SRC")/configure" "${vpx_args[@]}"
        make -j"$FF_JOBS"
        make install
    )
fi

if [ -n "${LIBAOM_SRC:-}" ]; then
    # libaom (BSD), AV1.
    cmake_part libaom "$LIBAOM_SRC" -DENABLE_TESTS=OFF -DENABLE_EXAMPLES=OFF \
        -DENABLE_DOCS=OFF -DENABLE_TOOLS=OFF -DENABLE_TESTDATA=OFF
fi

if [ -n "${SVTAV1_SRC:-}" ]; then
    # SVT-AV1 (BSD), the AV1 encoder only.
    # On x86, SVT-AV1 finds the SIMD instruction sets of the CPU only with
    # cpuinfo; without it, the encoder uses C code only. Its CMake script
    # downloads cpuinfo, which the disconnected build forbids, so the cpuinfo
    # part gives the same source. CPUINFO_LOG_TO_STDIO keeps cpuinfo from
    # linking the Android liblog, which the SVT-AV1 .pc file does not name.
    # On arm64, SVT-AV1 reads the CPU features itself, and does not use
    # cpuinfo.
    if [ "$x86" = 1 ]; then
        if [ -z "${CPUINFO_SRC:-}" ]; then
            echo "SVT-AV1 on x86 needs the cpuinfo part, which the recipe does not have." >&2
            exit 1
        fi
        svt_cpuinfo=(-DUSE_CPUINFO=LOCAL -DFETCHCONTENT_SOURCE_DIR_CPUINFO="$CPUINFO_SRC"
                     -DCPUINFO_LOG_TO_STDIO=ON)
    else
        svt_cpuinfo=(-DUSE_CPUINFO=OFF)
    fi
    cmake_part svtav1 "$SVTAV1_SRC" -DBUILD_APPS=OFF -DBUILD_DEC=OFF -DBUILD_TESTING=OFF \
        -DSVT_AV1_LTO=OFF "${svt_cpuinfo[@]}"
fi

if [ -n "${LIBWEBP_SRC:-}" ]; then
    # libwebp (BSD), with libwebpmux for animated WebP.
    cmake_part libwebp "$LIBWEBP_SRC" -DWEBP_BUILD_ANIM_UTILS=OFF -DWEBP_BUILD_CWEBP=OFF \
        -DWEBP_BUILD_DWEBP=OFF -DWEBP_BUILD_GIF2WEBP=OFF -DWEBP_BUILD_IMG2WEBP=OFF \
        -DWEBP_BUILD_VWEBP=OFF -DWEBP_BUILD_WEBPINFO=OFF -DWEBP_BUILD_WEBPMUX=OFF \
        -DWEBP_BUILD_EXTRAS=OFF -DWEBP_BUILD_LIBWEBPMUX=ON
fi

if [ -n "${OPENJPEG_SRC:-}" ]; then
    # OpenJPEG (BSD), JPEG 2000.
    cmake_part openjpeg "$OPENJPEG_SRC" -DBUILD_CODEC=OFF -DBUILD_TESTING=OFF \
        -DBUILD_STATIC_LIBS=ON
fi

if [ -n "${XVID_SRC:-}" ]; then
    # Xvid (GPL 2 or later), MPEG-4 Part 2. Its configure script has no
    # --disable-shared, so the shared library is removed after the install.
    xvid_dir=$(copy_source xvid "$XVID_SRC")
    refresh_config_scripts "$xvid_dir/build/generic"
    xvid_extra=()
    # The assembly code of Xvid needs an x86 assembler, and on AArch64 it has none.
    if [ "$x86" = 0 ] || [ "$FF_TARGET" = android ]; then xvid_extra+=(--disable-assembly); fi
    (
        cd "$xvid_dir/build/generic"
        env ${android_env[@]+"${android_env[@]}"} CFLAGS="-O2 -fPIC" ./configure \
            --prefix="$DEPS_MIXED" ${host_args[@]+"${host_args[@]}"} \
            ${xvid_extra[@]+"${xvid_extra[@]}"}
        # One job: the makefile of Xvid fails in parallel builds.
        make
        make install
    )
    # MinGW: the static library is xvidcore.a, and FFmpeg links -lxvidcore.
    if [ -f "$DEPS/lib/xvidcore.a" ]; then mv "$DEPS/lib/xvidcore.a" "$DEPS/lib/libxvidcore.a"; fi
    rm -f "$DEPS"/lib/libxvidcore.so* "$DEPS"/lib/libxvidcore*.dylib \
        "$DEPS"/lib/xvidcore.dll* "$DEPS"/bin/xvidcore.dll "$DEPS"/lib/xvidcore.dll.a
fi

if [ -n "${LIBOGG_SRC:-}" ]; then
    # libogg (BSD), for Vorbis and Theora.
    cmake_part libogg "$LIBOGG_SRC" -DBUILD_TESTING=OFF -DINSTALL_DOCS=OFF
fi

if [ -n "${LIBVORBIS_SRC:-}" ]; then
    # libvorbis (BSD).
    cmake_part libvorbis "$LIBVORBIS_SRC" -DBUILD_TESTING=OFF
fi

if [ -n "${LIBTHEORA_SRC:-}" ]; then
    # libtheora (BSD).
    theora_dir=$(copy_source libtheora "$LIBTHEORA_SRC")
    theora_extra=()
    if [ "$FF_TARGET" = android ] || [ "$x86" = 0 ]; then theora_extra+=(--disable-asm); fi
    autotools_part "$theora_dir" --disable-examples --disable-oggtest --disable-vorbistest \
        --disable-sdltest --disable-spec --disable-doc ${theora_extra[@]+"${theora_extra[@]}"}
fi

if [ -n "${LAME_SRC:-}" ]; then
    # LAME (LGPL 2 or later), MP3.
    lame_dir=$(copy_source lame "$LAME_SRC")
    refresh_config_scripts "$lame_dir"
    autotools_part "$lame_dir" --disable-frontend --disable-decoder --disable-gtktest \
        --disable-analyzer-hooks
fi

if [ -n "${OPUS_SRC:-}" ]; then
    # Opus (BSD). _FORTIFY_SOURCE and the stack protector can need libssp
    # with MinGW, which FFmpeg does not link.
    cmake_part opus "$OPUS_SRC" -DOPUS_BUILD_PROGRAMS=OFF -DOPUS_BUILD_TESTING=OFF \
        -DOPUS_FORTIFY_SOURCE=OFF -DOPUS_STACK_PROTECTOR=OFF
fi

if [ -n "${SOXR_SRC:-}" ]; then
    # SoX Resampler (LGPL 2.1 or later), for the aresample filter.
    cmake_part soxr "$SOXR_SRC" -DWITH_OPENMP=OFF -DBUILD_TESTS=OFF -DBUILD_EXAMPLES=OFF \
        -DWITH_LSR_BINDINGS=OFF
fi

if [ -n "${ZIMG_SRC:-}" ]; then
    # zimg (WTFPL), for the zscale filter. Its git tag has no configure script.
    zimg_dir=$(copy_source zimg "$ZIMG_SRC")
    # Homebrew installs libtoolize as glibtoolize; autoreconf reads LIBTOOLIZE.
    if ! command -v libtoolize > /dev/null && command -v glibtoolize > /dev/null; then
        export LIBTOOLIZE=glibtoolize
    fi
    (cd "$zimg_dir" && ./autogen.sh)
    # zimg.pc names STL_LIBS, which configure sets to -lstdc++ unless it is
    # given. macOS has no libstdc++, and Android needs the static libc++.
    STL_LIBS="$CXX_LIBS" autotools_part "$zimg_dir" --disable-testapp --disable-example \
        --disable-unit-test
fi

if [ -n "${VIDSTAB_SRC:-}" ]; then
    # vid.stab (GPL 2 or later), for the vidstab filters.
    cmake_part vidstab "$VIDSTAB_SRC" -DUSE_OMP=OFF
fi

if [ -n "${FREETYPE_SRC:-}" ]; then
    # FreeType (FTL OR GPL-2.0-or-later), for libass and the drawtext filter. Linux uses the
    # FreeType of the system instead.
    meson_part freetype "$FREETYPE_SRC" -Dbrotli=disabled -Dbzip2=disabled \
        -Dharfbuzz=disabled -Dpng=disabled -Dzlib=internal -Dtests=disabled
fi

if [ -n "${FRIBIDI_SRC:-}" ]; then
    # GNU FriBidi (LGPL 2.1 or later), for libass and drawtext.
    meson_part fribidi "$FRIBIDI_SRC" -Ddocs=false -Dbin=false -Dtests=false
fi

if [ -n "${HARFBUZZ_SRC:-}" ]; then
    # HarfBuzz (MIT), for libass and drawtext. It needs no C++ runtime.
    meson_part harfbuzz "$HARFBUZZ_SRC" -Dfreetype=enabled -Dglib=disabled \
        -Dgobject=disabled -Dcairo=disabled -Dchafa=disabled -Dicu=disabled \
        -Dgraphite2=disabled -Dpng=disabled -Dzlib=disabled -Dtests=disabled \
        -Dintrospection=disabled -Ddocs=disabled -Dutilities=disabled \
        -Dbenchmark=disabled -Dsubset=disabled -Draster=disabled -Dvector=disabled \
        -Dgpu=disabled -Dgpu_demo=disabled -Dwasm=disabled -Dfontations=disabled \
        -Dharfrust=disabled -Dkbts=disabled
fi

if [ -n "${LIBASS_SRC:-}" ]; then
    # libass (ISC), for the subtitles and ass filters. Its font provider is
    # Fontconfig on Linux, DirectWrite on Windows, and CoreText on macOS;
    # Android has none, so libass uses the fonts of the subtitle file, the
    # font attachments of the media file, and the fonts that the application
    # adds (fontsdir of the subtitles filter).
    ass_fonts=(-Dfontconfig=disabled -Ddirectwrite=disabled -Dcoretext=disabled)
    case "$FF_TARGET" in
        linux) ass_fonts=(-Dfontconfig=enabled -Ddirectwrite=disabled -Dcoretext=disabled) ;;
        mingw) ass_fonts=(-Dfontconfig=disabled -Ddirectwrite=enabled -Dcoretext=disabled) ;;
        macos) ass_fonts=(-Dfontconfig=disabled -Ddirectwrite=disabled -Dcoretext=enabled) ;;
    esac
    ass_asm=disabled
    if [ "$x86" = 1 ]; then ass_asm=enabled; fi
    ass_src="$LIBASS_SRC"
    if [ "$FF_TARGET" = mingw ]; then
        # libass links any iconv that meson finds, and has no option to turn
        # it off. MinGW 13.1 has only a static GNU libiconv (LGPL), whose
        # license and source the package does not carry, as for the
        # --disable-iconv of FFmpeg. libass only loses the recoding of
        # subtitle files that are not UTF-8.
        ass_src=$(copy_source libass "$LIBASS_SRC")
        grep -q "dependency('iconv', required: false)" "$ass_src/meson.build" ||
            { echo "libass/meson.build has no iconv dependency line to remove" >&2; exit 1; }
        sed -i "s/dependency('iconv', required: false)/dependency('', required: false)/" \
            "$ass_src/meson.build"
        ass_src=$(mixed_path "$ass_src")
    fi
    meson_part libass "$ass_src" "${ass_fonts[@]}" "-Dasm=$ass_asm" \
        -Dlibunibreak=disabled -Drequire-system-font-provider=false -Dtest=disabled \
        -Dcompare=disabled -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled
fi

if [ -n "${MBEDTLS_SRC:-}" ]; then
    # Mbed TLS (Apache 2.0 OR GPL 2.0 or later), the encryption of SRT.
    cmake_part mbedtls "$MBEDTLS_SRC" -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF \
        -DUSE_SHARED_MBEDTLS_LIBRARY=OFF -DUSE_STATIC_MBEDTLS_LIBRARY=ON \
        -DMBEDTLS_FATAL_WARNINGS=OFF -DGEN_FILES=OFF
fi

if [ -n "${SRT_SRC:-}" ]; then
    # SRT (MPL 2.0), the srt protocol, encrypted with Mbed TLS. The C++
    # standard library replaces pthreads for its threads.
    cmake_part srt "$SRT_SRC" -DENABLE_APPS=OFF -DENABLE_SHARED=OFF -DENABLE_STATIC=ON \
        -DUSE_ENCLIB=mbedtls -DENABLE_STDCXX_SYNC=ON -DENABLE_UNITTESTS=OFF
    # srt.pc names the Mbed TLS archives by absolute path. FFmpeg's configure
    # puts such paths before -lsrt, so GNU ld reads them too early; -l names
    # keep the order. Other absolute archives are runtime libraries of the
    # compiler (with the NDK, libclang_rt.builtins), which the compiler links
    # anyway; their paths are of the build machine, so they are removed.
    # The SRT CMake script finds the archives as <deps>/lib/../lib/libX.a.
    sed -E -i.bak -e "s#$DEPS_MIXED/lib/(\.\./lib/)*lib([A-Za-z0-9_+-]+)\.a#-l\2#g" \
        -e "s#$DEPS/lib/(\.\./lib/)*lib([A-Za-z0-9_+-]+)\.a#-l\2#g" "$DEPS/lib/pkgconfig/srt.pc"
    for _ in 1 2; do
        sed -E -i.bak 's#(^|[[:space:]])([A-Za-z]:)?/[^[:space:]]*\.a([[:space:]]|$)#\1\3#g' \
            "$DEPS/lib/pkgconfig/srt.pc"
    done
    rm -f "$DEPS/lib/pkgconfig/srt.pc.bak"
    if ! grep -Eq '(^|[[:space:]])-lmbedcrypto([[:space:]]|$)' "$DEPS/lib/pkgconfig/srt.pc"; then
        echo "srt.pc does not name Mbed TLS (-lmbedcrypto), which libsrt needs:" >&2
        cat "$DEPS/lib/pkgconfig/srt.pc" >&2
        exit 1
    fi
    # srt.pc copies the implicit libraries of the C++ compiler. Android needs
    # the static C++ runtime instead. MinGW must not link libgcc_s (a DLL) or
    # the GNU libiconv of MinGW, and Mbed TLS needs bcrypt and Winsock. Each
    # token is removed whole, also when two are next to each other.
    if [ "$FF_TARGET" = android ] || [ "$FF_TARGET" = mingw ]; then
        bad='-l(c\+\+(_shared)?|stdc\+\+|gcc_s|gcc|iconv)'
        for _ in 1 2; do
            sed -E -i "s/(^|[[:space:]])$bad([[:space:]]|\$)/\\1\\4/g" "$DEPS/lib/pkgconfig/srt.pc"
        done
        if grep -Eq "(^|[[:space:]])$bad([[:space:]]|\$)" "$DEPS/lib/pkgconfig/srt.pc"; then
            echo "srt.pc still names a library that the package must not link:" >&2
            cat "$DEPS/lib/pkgconfig/srt.pc" >&2
            exit 1
        fi
        if [ "$FF_TARGET" = mingw ]; then
            add_private_libs srt "$CXX_LIBS -lws2_32 -lbcrypt"
        else
            add_private_libs srt "$CXX_LIBS"
        fi
    fi
fi

if [ "$FF_TARGET" = android ] && [ -d "$DEPS/lib/pkgconfig" ]; then
    # Bionic has the pthread functions, and the NDK has no libpthread, but
    # some parts (libvpx, SVT-AV1) write -lpthread into their .pc files.
    for pc in "$DEPS"/lib/pkgconfig/*.pc; do
        for _ in 1 2; do
            sed -E -i 's/(^|[[:space:]])-lpthread([[:space:]]|$)/\1\2/g' "$pc"
        done
    done
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
if [ "$license_line" != "#define FFMPEG_LICENSE \"$FF_LICENSE\"" ]; then
    echo "FFmpeg is not $FF_LICENSE: $license_line" >&2
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
        -e "s| *-L$DEPS/lib[^ ]*||g" \
        -e "s|$DEPS_MIXED/lib/lib\([A-Za-z0-9_+-]*\)\.a|-l\1|g" \
        -e "s|$DEPS/lib/lib\([A-Za-z0-9_+-]*\)\.a|-l\1|g" "$pc" > "$pc.tmp"
    mv "$pc.tmp" "$pc"
done

# The parts are inside the FFmpeg libraries; their files are not needed.
rm -rf "$OUT/share/ffmpeg" "$OUT/share/man"
echo "FFmpeg for $FF_TARGET installed in $OUT"
