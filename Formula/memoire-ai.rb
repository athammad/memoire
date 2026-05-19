# Homebrew formula for memoire-ai.
#
# Install via:
#   brew tap athammad/memoire
#   brew install memoire-ai
#
# When a versioned PyPI release is available, update `url` and `sha256`
# to point to the sdist tarball from https://pypi.org/pypi/memoire-ai/json

class MemoireAi < Formula
  include Language::Python::Virtualenv

  desc "Persistent causal memory for AI coding assistants"
  homepage "https://athammad.github.io/memoire"
  url "https://github.com/athammad/memoire/archive/refs/heads/master.tar.gz"
  version "0.1.0"
  sha256 "5298baba4cf2c901537d89d4370f0ba1fa3c3b4e64bae0957e351254082d1ac1"
  license "MIT"
  head "https://github.com/athammad/memoire.git", branch: "master"

  depends_on "python@3.12"

  # Core runtime dependencies (mirrors pyproject.toml)
  resource "surrealdb" do
    url "https://files.pythonhosted.org/packages/source/s/surrealdb/surrealdb-2.0.0.tar.gz"
    sha256 "70a2656bf5b6d844cf4c029d6e0a7309ca95b9ba282663f31c8b6e642fc8463a"
  end

  resource "watchdog" do
    url "https://files.pythonhosted.org/packages/source/w/watchdog/watchdog-6.0.0.tar.gz"
    sha256 "9ddf7c82fda3ae8e24decda1338ede66e1c99883db93711d8fb941eaa2d8c282"
  end

  resource "mcp" do
    url "https://files.pythonhosted.org/packages/source/m/mcp/mcp-1.0.0.tar.gz"
    sha256 "dba51ce0b5c6a80e25576f606760c49a91ee90210fed805b530ca165d3bbc9b7"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.0.0.tar.gz"
    sha256 "7d8c289ee437bcb0316820ccee14aefcb056e58d31830ecab8e47eda6540e136"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.0.tar.gz"
    sha256 "a0cb88a46f32dc874e04ee956e4c2764aba2aa228f650b06788ba6bda2962ab5"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      memoire requires SurrealDB to be running as a backend.
      Install SurrealDB separately:
        curl -sSf https://install.surrealdb.com | sh

      Then initialise memoire in your project:
        memoire init --provider claude
        memoire ingest
        memoire install-service
    EOS
  end

  test do
    assert_match "Usage:", shell_output("#{bin}/memoire --help")
  end
end
