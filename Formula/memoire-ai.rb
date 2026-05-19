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
  sha256 :no_check
  license "MIT"
  head "https://github.com/athammad/memoire.git", branch: "master"

  depends_on "python@3.12"

  # Core runtime dependencies (mirrors pyproject.toml)
  resource "surrealdb" do
    url "https://files.pythonhosted.org/packages/source/s/surrealdb/surrealdb-2.0.0.tar.gz"
    sha256 :no_check
  end

  resource "watchdog" do
    url "https://files.pythonhosted.org/packages/source/w/watchdog/watchdog-6.0.0.tar.gz"
    sha256 :no_check
  end

  resource "mcp" do
    url "https://files.pythonhosted.org/packages/source/m/mcp/mcp-1.0.0.tar.gz"
    sha256 :no_check
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.0.0.tar.gz"
    sha256 :no_check
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.0.tar.gz"
    sha256 :no_check
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
