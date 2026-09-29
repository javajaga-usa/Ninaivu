# The Homebrew cask. build.sh fills in the version and this architecture's
# hash under build/homebrew/; the release workflow fills the other one from
# the other runner's build and opens the pull request against
# Homebrew/homebrew-cask (or the project's own tap while the app is new).
cask "ninaivu" do
  arch arm: "arm64", intel: "x86_64"

  version "__VERSION__"
  sha256 arm:   "__SHA256_ARM64__",
         intel: "__SHA256_X86_64__"

  url "https://github.com/javajaga-usa/Ninaivu/releases/download/v#{version}/Ninaivu-#{version}-macos-#{arch}.dmg"
  name "Ninaivu"
  desc "Private, self-hosted family photo library with faces, places, search and an encrypted cloud copy"
  homepage "https://github.com/javajaga-usa/Ninaivu"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :monterey"

  app "Ninaivu.app"

  uninstall launchctl: "local.ninaivu.start",
            quit:      "org.ninaivu.app"

  zap trash: [
    "~/Library/Application Support/Ninaivu",
    "~/Library/LaunchAgents/local.ninaivu.start.plist",
  ]
end
