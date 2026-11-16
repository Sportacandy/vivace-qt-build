# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate .github/workflows/kit.yml.

Usage: python tools/gen-kit-workflow.py > .github/workflows/kit.yml

The stage jobs s1..sN are identical except for their numbers, so this script
writes them. Change STAGES to allow longer builds on slow runners.
"""
import sys

STAGES = 12

HEAD = r'''# Builds one Qt kit. Called by build.yml.
#
# GENERATED FILE. Do not edit. Change tools/gen-kit-workflow.py, then run:
#   python tools/gen-kit-workflow.py > .github/workflows/kit.yml
# It writes {n} stage jobs (s1..s{n}) that are identical except for their numbers.
#
# A GitHub-hosted job stops after 6 hours. A full Qt build is longer, so the
# work is split into chained stage jobs. Each stage restores the install
# prefix that the previous stage saved, builds more modules until its time
# budget is used, and saves the prefix again. A stage runs only if the
# previous stage left work. "Re-run failed jobs" continues from the last
# saved state.
#
# Each attempt of each job saves the state as its own artifact,
# state-<kit>-a<attempt of prepare>-<job>-r<attempt of this job>, and passes the name of the
# newest state to the next job. A failed upload loses only the work of that
# attempt, never a state that an earlier job or attempt saved. package
# publishes the final state as host-qt-<qt_version>-<kit> for kits that are a
# host Qt (the Android kits use host-qt-<qt_version>-mingw1310_64). publish
# then deletes the per-job artifacts.
#
# If webengine_runner is set, the stage jobs leave out QtWebEngine and the
# modules that use it. The webengine job builds them after the last stage,
# on that runner (normally self-hosted, without the 6-hour limit).

name: Build one kit

on:
  workflow_call:
    inputs:
      kit:
        description: Kit name from versions/<qt_version>.json.
        type: string
        required: true
      qt_version:
        type: string
        required: true
      runner:
        description: Runner label, or a JSON array of labels.
        type: string
        required: true
      stage_minutes:
        description: timeout-minutes of each stage job (at most 360 on GitHub-hosted runners).
        type: number
        default: 350
      docs:
        description: kit-default, on or off.
        type: string
        default: kit-default
      webengine:
        type: boolean
        default: false
      webengine_runner:
        description: >-
          Runner label, or a JSON array of labels, for the webengine job. Empty
          means that the stage jobs build QtWebEngine.
        type: string
        default: ""
      webengine_minutes:
        description: timeout-minutes of the webengine job.
        type: number
        default: 1380
      skip_modules:
        description: Comma-separated modules to leave out.
        type: string
        default: ""
      release_tag:
        description: If set, upload the archive to this GitHub release.
        type: string
        default: ""
      host_run_id:
        description: Run that has the host Qt artifact (Android kits). Empty means this run.
        type: string
        default: ""

# Read-only by default (actions: read lists and downloads artifacts of other
# runs). Only publish writes: it creates the release and deletes artifacts.
permissions:
  contents: read
  actions: read

jobs:
  prepare:
    runs-on: ubuntu-24.04
    outputs:
      state_prefix: ${{{{ steps.names.outputs.state_prefix }}}}
      publish_state: ${{{{ steps.plan.outputs.publish_state }}}}
      runs_on: ${{{{ steps.runner.outputs.runs_on }}}}
      webengine_runs_on: ${{{{ steps.runner.outputs.webengine_runs_on }}}}
      host: ${{{{ steps.plan.outputs.host }}}}
      toolchain: ${{{{ steps.plan.outputs.toolchain }}}}
      host_kit: ${{{{ steps.plan.outputs.host_kit }}}}
      webengine: ${{{{ steps.plan.outputs.webengine }}}}
      offload: ${{{{ steps.plan.outputs.offload }}}}
      ffmpeg_key: ${{{{ steps.plan.outputs.ffmpeg_key }}}}
      ffmpeg_runs_on: ${{{{ steps.ffmpeg.outputs.runs_on }}}}
    steps:
      - uses: actions/checkout@v7
        with:
          # The build runs third-party code (Qt, Chromium, pip packages).
          # Do not leave the job token in .git/config.
          persist-credentials: false

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      # The attempt number keeps the artifacts of "Re-run all jobs" apart
      # from the artifacts of the earlier attempt.
      - name: Name the state artifacts
        id: names
        env:
          KIT: ${{{{ inputs.kit }}}}
        run: echo "state_prefix=state-$KIT-a$GITHUB_RUN_ATTEMPT" >> "$GITHUB_OUTPUT"

      - name: Resolve the runners
        id: runner
        env:
          RUNNER_INPUT: ${{{{ inputs.runner }}}}
          WEBENGINE_RUNNER_INPUT: ${{{{ inputs.webengine_runner }}}}
        run: |
          python - <<'EOF' >> "$GITHUB_OUTPUT"
          import json, os
          def labels(value):
              return value if value.startswith("[") else json.dumps(value)
          runs_on = labels(os.environ["RUNNER_INPUT"].strip())
          # Without a webengine runner the webengine job is skipped; the main
          # runner keeps its runs-on value valid.
          webengine = os.environ["WEBENGINE_RUNNER_INPUT"].strip()
          print("runs_on=" + runs_on)
          print("webengine_runs_on=" + (labels(webengine) if webengine else runs_on))
          EOF

      - name: Plan the build
        id: plan
        env:
          GITHUB_TOKEN: ${{{{ github.token }}}}
          KIT: ${{{{ inputs.kit }}}}
          QT_VERSION: ${{{{ inputs.qt_version }}}}
          DOCS: ${{{{ inputs.docs }}}}
          WEBENGINE: ${{{{ inputs.webengine }}}}
          WEBENGINE_RUNNER: ${{{{ inputs.webengine_runner }}}}
          SKIP: ${{{{ inputs.skip_modules }}}}
          STAGE_MINUTES: ${{{{ inputs.stage_minutes }}}}
        run: |
          args=(--qt-version "$QT_VERSION" --kit "$KIT" --docs "$DOCS" --skip "$SKIP" --state "$RUNNER_TEMP/state"
                --stage-minutes "$STAGE_MINUTES")
          if [ "$WEBENGINE" = "true" ]; then args+=(--webengine); fi
          if [ -n "${{WEBENGINE_RUNNER//[[:space:]]/}}" ]; then args+=(--offload-webengine); fi
          python tools/qtci.py plan "${{args[@]}}"

      # The ffmpeg job runs on the runner of the kit, so that FFmpeg is built
      # with the compiler of the kit. The Android NDK cross-compiles on Linux.
      - name: Pick the FFmpeg runner
        id: ffmpeg
        env:
          TOOLCHAIN: ${{{{ steps.plan.outputs.toolchain }}}}
          RUNS_ON: ${{{{ steps.runner.outputs.runs_on }}}}
        run: |
          if [ "$TOOLCHAIN" = android ]; then
            echo 'runs_on="ubuntu-24.04"' >> "$GITHUB_OUTPUT"
          else
            echo "runs_on=$RUNS_ON" >> "$GITHUB_OUTPUT"
          fi

      - name: Save the build plan
        uses: actions/upload-artifact@v7
        with:
          name: ${{{{ steps.names.outputs.state_prefix }}}}-prepare
          path: ${{{{ runner.temp }}}}/state
          overwrite: true
          retention-days: 7

  # Builds the LGPL FFmpeg (tools/build-ffmpeg.sh) that the stage jobs use.
  # The cache keeps it for later runs until the recipe in
  # versions/<qt_version>.json or the build script changes. ffmpeg.yml
  # builds and releases the same FFmpeg with the same cache key.
  ffmpeg:
    needs: [prepare]
    runs-on: ${{{{ fromJSON(needs.prepare.outputs.ffmpeg_runs_on) }}}}
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v7
        with:
          # The build runs third-party code (FFmpeg, dav1d, pip packages).
          # Do not leave the job token in .git/config.
          persist-credentials: false

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: Download the build plan
        uses: actions/download-artifact@v8
        with:
          name: ${{{{ needs.prepare.outputs.state_prefix }}}}-prepare
          path: ${{{{ runner.temp }}}}/state

      - uses: ./.github/actions/build-ffmpeg
        with:
          state-dir: ${{{{ runner.temp }}}}/state
          toolchain: ${{{{ needs.prepare.outputs.toolchain }}}}
          cache-key: ${{{{ needs.prepare.outputs.ffmpeg_key }}}}
          out: ${{{{ runner.temp }}}}/ffmpeg

      # The LGPL asks that the source is offered from the same place as the
      # libraries. The source archive is uploaded first, so that no artifact
      # has FFmpeg without its source. It is kept longer than the artifacts
      # that contain FFmpeg (this one, the states, and the host Qt), and the
      # package job puts it next to the kit archive.
      - name: Make the FFmpeg source archive
        shell: bash
        run: |
          # exec: on a cancel, Python gets the signal.
          exec python tools/qtci.py ffmpeg-source --state "$RUNNER_TEMP/state" \
            --out "$RUNNER_TEMP/ffmpeg-source"

      - name: Save the FFmpeg source archive
        uses: actions/upload-artifact@v7
        with:
          name: ffmpeg-source-${{{{ inputs.kit }}}}
          path: ${{{{ runner.temp }}}}/ffmpeg-source
          overwrite: true
          compression-level: 0
          retention-days: 14

      - name: Save the FFmpeg
        uses: actions/upload-artifact@v7
        with:
          name: ffmpeg-${{{{ inputs.kit }}}}
          path: ${{{{ runner.temp }}}}/ffmpeg
          overwrite: true
          retention-days: 7
'''

STAGE = r'''
  s{i}:
    needs: [prepare{prev_needs}]
{cond}    runs-on: ${{{{ fromJSON(needs.prepare.outputs.runs_on) }}}}
    timeout-minutes: ${{{{ inputs.stage_minutes }}}}
    outputs:
      remaining: ${{{{ steps.stage.outputs.remaining }}}}
      state_name: ${{{{ steps.stage.outputs.state-name }}}}
    steps:
      - uses: actions/checkout@v7
        with:
          # The build runs third-party code (Qt, Chromium, pip packages).
          # Do not leave the job token in .git/config.
          persist-credentials: false
      - id: stage
        uses: ./.github/actions/stage
        with:
          kit: ${{{{ inputs.kit }}}}
          host: ${{{{ needs.prepare.outputs.host }}}}
          toolchain: ${{{{ needs.prepare.outputs.toolchain }}}}
          webengine: ${{{{ needs.prepare.outputs.webengine }}}}
          host-kit: ${{{{ needs.prepare.outputs.host_kit }}}}
          host-run-id: ${{{{ inputs.host_run_id }}}}
          state-from: {state_from}
          state-name: ${{{{ needs.prepare.outputs.state_prefix }}}}-s{i}
          fail-if-remaining: "{last}"
          job-minutes: ${{{{ inputs.stage_minutes }}}}
'''

TAIL = r'''
  # Builds QtWebEngine and the modules that use it, after all stage jobs.
  webengine:
    needs: [prepare, ffmpeg, {all_stages}]
    if: ${{{{ !cancelled() && !contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled') && needs.prepare.outputs.offload == 'true' }}}}
    runs-on: ${{{{ fromJSON(needs.prepare.outputs.webengine_runs_on) }}}}
    timeout-minutes: ${{{{ inputs.webengine_minutes }}}}
    outputs:
      state_name: ${{{{ steps.stage.outputs.state-name }}}}
    steps:
      - uses: actions/checkout@v7
        with:
          # The build runs third-party code (Qt, Chromium, pip packages).
          # Do not leave the job token in .git/config.
          persist-credentials: false
      - id: stage
        uses: ./.github/actions/stage
        with:
          kit: ${{{{ inputs.kit }}}}
          host: ${{{{ needs.prepare.outputs.host }}}}
          toolchain: ${{{{ needs.prepare.outputs.toolchain }}}}
          webengine: "true"
          pool: webengine
          state-from: ${{{{ {stages_state} }}}}
          state-name: ${{{{ needs.prepare.outputs.state_prefix }}}}-webengine
          fail-if-remaining: "true"
          job-minutes: ${{{{ inputs.webengine_minutes }}}}

  package:
    needs: [prepare, ffmpeg, {all_stages}, webengine]
    if: ${{{{ !cancelled() && !contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled') }}}}
    runs-on: ${{{{ fromJSON(needs.prepare.outputs.runs_on) }}}}
    timeout-minutes: 180
    # Read-only: this runner can be self-hosted and ran third-party build
    # code. The publish job does the writes on a new GitHub-hosted machine.
    steps:
      - uses: actions/checkout@v7
        with:
          # The build runs third-party code (Qt, Chromium, pip packages).
          # Do not leave the job token in .git/config.
          persist-credentials: false

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: Install archive tools
        shell: bash
        run: |
          # GitHub-hosted images have all of these tools. A new self-hosted
          # runner can lack them. choco exit codes 3010 and 1641 only ask
          # for a reboot.
          choco_install() {{
            local rc=0
            choco install -y --no-progress "$@" || rc=$?
            [ "$rc" -eq 0 ] || [ "$rc" -eq 3010 ] || [ "$rc" -eq 1641 ]
          }}
          case "${{{{ needs.prepare.outputs.host }}}}" in
            windows)
              command -v zstd || choco_install zstandard
              command -v 7z || choco_install 7zip
              printf '%s\n' 'C:\ProgramData\chocolatey\bin' >> "$GITHUB_PATH" ;;
            linux)
              if ! command -v zstd; then
                sudo apt-get update -q && sudo apt-get install -y -q --no-upgrade zstd
              fi ;;
            macos)
              command -v zstd || HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 \
                HOMEBREW_NO_INSTALL_CLEANUP=1 brew install zstd ;;
          esac
          if [ "${{{{ needs.prepare.outputs.host }}}}" != "windows" ]; then
            root="${{QTCI_PREFIX_ROOT:-}}"
            if [ -z "$root" ] && [ "${{RUNNER_ENVIRONMENT:-}}" = self-hosted ]; then
              echo "::error::Set QTCI_PREFIX_ROOT on this self-hosted runner. The build deletes files in it."
              exit 1
            fi
            if [ -z "$root" ]; then
              if [ "${{{{ needs.prepare.outputs.host }}}}" = "macos" ]; then root="$HOME/Qt"; else root=/opt/Qt; fi
            fi
            if ! {{ mkdir -p "$root" 2>/dev/null && [ -w "$root" ]; }}; then
              sudo mkdir -p "$root" && sudo chown "$(id -u):$(id -g)" "$root"
            fi
          fi

      - name: Download the build state
        uses: actions/download-artifact@v8
        with:
          name: ${{{{ needs.webengine.outputs.state_name || {stages_state} }}}}
          path: ${{{{ runner.temp }}}}/state

      - name: Make the archive
        shell: bash
        env:
          RELEASE_TAG: ${{{{ inputs.release_tag }}}}
          PUBLISH_STATE: ${{{{ needs.prepare.outputs.publish_state }}}}
        run: |
          # A GitHub release asset must be smaller than 2 GiB.
          split=0
          if [ -n "$RELEASE_TAG" ]; then split=1900; fi
          # The state has the FFmpeg source archive (setup-kit put it there).
          args=(--state "$RUNNER_TEMP/state" --out dist --split-mib "$split"
                --ffmpeg-source "$RUNNER_TEMP/state")
          if [ "$PUBLISH_STATE" = "true" ]; then args+=(--keep-state); fi
          # exec: the runner signals the shell of the step when the job is
          # cancelled; Python must get the signal to stop its child processes.
          exec python tools/qtci.py package "${{args[@]}}"

      # Other kits use this kit as their host Qt (QT_HOST_PATH), in this run
      # or in a later run through host_run_id.
      - name: Publish the final state
        if: needs.prepare.outputs.publish_state == 'true'
        uses: actions/upload-artifact@v7
        with:
          # The name has the Qt version, so that a later run of another Qt
          # version cannot use it through host_run_id.
          name: host-qt-${{{{ inputs.qt_version }}}}-${{{{ inputs.kit }}}}
          path: ${{{{ runner.temp }}}}/state
          overwrite: true
          compression-level: 0
          retention-days: 7

      - name: Upload the archive
        uses: actions/upload-artifact@v7
        with:
          name: qt-${{{{ inputs.qt_version }}}}-${{{{ inputs.kit }}}}
          path: dist
          overwrite: true
          compression-level: 0
          retention-days: 14

  # The only job with a write token. It runs on a new GitHub-hosted machine
  # and runs no code from the build: it only downloads the archive, uploads
  # it to the release, and deletes the per-job states.
  # !cancelled(): without a status function, GitHub adds success(), which is
  # false when any earlier job was skipped (the unused stages always are).
  publish:
    needs: [package]
    if: ${{{{ !cancelled() && needs.package.result == 'success' }}}}
    runs-on: ubuntu-24.04
    timeout-minutes: 120
    permissions:
      contents: write
      actions: write
    steps:
      - name: Free disk space for the archive
        if: inputs.release_tag != ''
        run: |
          sudo rm -rf /usr/share/dotnet /opt/ghc /usr/local/.ghcup /usr/local/share/boost \
            /opt/hostedtoolcache/CodeQL /usr/local/lib/android || true

      - name: Download the archive
        if: inputs.release_tag != ''
        uses: actions/download-artifact@v8
        with:
          name: qt-${{{{ inputs.qt_version }}}}-${{{{ inputs.kit }}}}
          path: dist

      - name: Upload to the release
        if: inputs.release_tag != ''
        shell: bash
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          GH_REPO: ${{{{ github.repository }}}}
          RELEASE_TAG: ${{{{ inputs.release_tag }}}}
          QT_VERSION: ${{{{ inputs.qt_version }}}}
        run: |
          # A tag that starts with "-" would be read as a gh option.
          if ! [[ "$RELEASE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
            echo "::error::release_tag must start with a letter or digit and contain only letters, digits, '.', '_', '+', and '-'."
            exit 1
          fi
          if ! gh release view "$RELEASE_TAG" > /dev/null 2>&1; then
            notes="OSS Qt $QT_VERSION built with the patches in patches/$QT_VERSION/. Build: $GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
            # --target tags the commit that this run built, not the newest
            # commit of the default branch.
            gh release create "$RELEASE_TAG" --target "$GITHUB_SHA" \
              --title "Qt $QT_VERSION ($RELEASE_TAG)" --notes "$notes" \
              || gh release view "$RELEASE_TAG" > /dev/null
          fi
          # Delete the assets of an earlier upload of this archive first. A
          # split into fewer parts would otherwise leave old parts behind.
          for sum in dist/*.sha256; do
            archive=$(basename "$sum" .sha256)
            gh release view "$RELEASE_TAG" --json assets --jq '.assets[].name' |
            while read -r asset; do
              case "$asset" in
                "$archive"*) gh release delete-asset "$RELEASE_TAG" "$asset" --yes ;;
              esac
            done
          done
          # The FFmpeg source first: the release must not show FFmpeg without
          # its source, also if a later upload fails.
          for src in dist/*-ffmpeg-source.tar; do
            if [ -e "$src" ]; then
              gh release upload "$RELEASE_TAG" "$src" --clobber
            fi
          done
          # Only once: a second --clobber upload would delete the source of
          # the public release for a moment.
          find dist -maxdepth 1 -type f ! -name '*-ffmpeg-source.tar' -print0 |
            xargs -0 gh release upload "$RELEASE_TAG" --clobber

      # The per-job states are not needed after a successful package. A
      # failure here only leaves them until they expire.
      - name: Delete the per-job states
        continue-on-error: true
        shell: bash
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          KIT: ${{{{ inputs.kit }}}}
        run: |
          gh api --paginate "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/artifacts?per_page=100" \
            --jq ".artifacts[] | select(.name | startswith(\"state-$KIT-a\")) | .id" |
          while read -r id; do
            gh api -X DELETE "repos/$GITHUB_REPOSITORY/actions/artifacts/$id"
          done
'''


def main() -> None:
    out = [HEAD.format(n=STAGES)]
    for i in range(1, STAGES + 1):
        if i == 1:
            prev_needs, cond = ", ffmpeg", ""
            state_from = "${{ needs.prepare.outputs.state_prefix }}-prepare"
        else:
            prev_needs = f", s{i - 1}"
            cond = f"    if: needs.s{i - 1}.outputs.remaining != '0'\n"
            state_from = f"${{{{ needs.s{i - 1}.outputs.state_name }}}}"
        out.append(STAGE.format(i=i, prev_needs=prev_needs, cond=cond,
                                state_from=state_from, last=str(i == STAGES).lower()))
    # The newest state of the stage jobs: the last stage that ran. A skipped
    # stage has an empty output.
    stages_state = " || ".join(f"needs.s{i}.outputs.state_name" for i in range(STAGES, 0, -1))
    out.append(TAIL.format(all_stages=", ".join(f"s{i}" for i in range(1, STAGES + 1)),
                           stages_state=stages_state))
    sys.stdout.write("".join(out))


main()
