#!/bin/zsh

project_root=$PWD

usage() {
    echo -e "Usage: $(basename "$0") [-t type] [-c version] [-p prerelease] [-n]"
    echo -e "  -t: Bump version by type (major, minor, patch)"
    echo -e "  -c: Set specific version"
    echo -e "  -p: Create pre-release version (alpha, beta, rc)"
    echo -e "  -n: Compute and set next version using hatch"
}

bump_web() {
    cd "$project_root/src/interface/web" || exit 1
    yarn version "$@" --no-git-tag-version
}

bump_obsidian() {
    cd "$project_root/src/interface/obsidian" || exit 1
    yarn version "$@" --no-git-tag-version
    cp "$project_root/versions.json" .
    yarn run version
    cd "$project_root" || exit 1
    cp src/interface/obsidian/versions.json .
    cp src/interface/obsidian/manifest.json .
}

stage_release_files() {
    git add \
        "$project_root/src/interface/web/package.json" \
        "$project_root/src/interface/obsidian/package.json" \
        "$project_root/src/interface/obsidian/yarn.lock" \
        "$project_root/src/interface/obsidian/manifest.json" \
        "$project_root/src/interface/obsidian/versions.json" \
        "$project_root/manifest.json" \
        "$project_root/versions.json"
}

while getopts 'nc:t:p:' opt;
do
    case "${opt}" in
        p)
            prerelease_type=$OPTARG
            cd "$project_root/src/interface/web" || exit 1
            current_base_version=$(grep '"version":' package.json | awk -F '"' '{print $4}')
            base_version=$(echo "$current_base_version" | sed 's/-.*$//')
            if [[ $current_base_version == *"-$prerelease_type"* ]]; then
                current_num=$(echo "$current_base_version" | sed "s/.*-$prerelease_type\.//" | sed 's/[^0-9]*$//')
                next_num=$((current_num + 1))
                current_version="$base_version-$prerelease_type.$next_num"
            elif [[ $base_version == 1.* ]]; then
                current_version="2.0.0-$prerelease_type.1"
            else
                current_version="$base_version-$prerelease_type.1"
            fi

            bump_web --new-version "$current_version"
            cd "$project_root/src/interface/obsidian" || exit 1
            yarn build
            bump_obsidian --new-version "$current_version"
            pre-commit run --hook-stage manual --all
            stage_release_files
            git commit -m "Release Khoj version $current_version"
            git tag "$current_version"
            ;;
        t)
            version_type=$OPTARG
            bump_web "--$version_type"
            cd "$project_root/src/interface/web" || exit 1
            current_version=$(grep '"version":' package.json | awk -F '"' '{print $4}')
            cd "$project_root/src/interface/obsidian" || exit 1
            yarn build
            bump_obsidian "--$version_type"
            pre-commit run --hook-stage manual --all
            stage_release_files
            git commit -m "Release Khoj version $current_version"
            git tag "$current_version"
            ;;
        c)
            current_version=$OPTARG
            bump_web --new-version "$current_version"
            bump_obsidian --new-version "$current_version"
            pre-commit run --hook-stage manual --all
            stage_release_files
            git commit -m "Release Khoj version $current_version"
            git tag "$current_version"
            ;;
        n)
            next_version=$(touch bump.txt && git add bump.txt && hatch version | sed 's/\.dev.*//g')
            git rm --cached -- bump.txt && rm bump.txt
            bump_web --new-version "$next_version"
            cd "$project_root/src/interface/obsidian" || exit 1
            git rm --cached -- versions.json
            bump_obsidian --new-version "$next_version"
            pre-commit run --hook-stage manual --all
            stage_release_files
            git commit -m "Bump Khoj to pre-release version $next_version"
            ;;
        ?)
            usage
            exit 1
            ;;
    esac
done

cd "$project_root" || exit 1
