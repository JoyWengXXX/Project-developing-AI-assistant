import pytest

from repopilot.errors import InvalidGitHubRepoError
from repopilot.github.repo_ref import GitHubRepoRef, is_valid_owner_and_name, parse_github_repo


@pytest.mark.parametrize(
    ("raw", "owner", "name"),
    [
        ("octocat/Hello-World", "octocat", "Hello-World"),
        ("a/b", "a", "b"),
        ("A1-b2/x.y_z-1", "A1-b2", "x.y_z-1"),
        ("o/" + "n" * 100, "o", "n" * 100),
        ("o" * 39 + "/x", "o" * 39, "x"),
        ("https://github.com/octocat/Hello-World", "octocat", "Hello-World"),
        ("https://github.com/octocat/Hello-World.git", "octocat", "Hello-World"),
        ("https://github.com/octocat/Hello-World/", "octocat", "Hello-World"),
        ("https://github.com/octocat/Hello-World.git/", "octocat", "Hello-World"),
        ("HTTPS://GitHub.COM/octocat/Hello-World", "octocat", "Hello-World"),
        ("https://github.com/octocat/vue.js", "octocat", "vue.js"),
        ("octocat/git-tools", "octocat", "git-tools"),
        ("octocat/legit", "octocat", "legit"),
        # GitHub 允許以 `.` 開頭的名稱（本專案使用者的實際 repo 即為此類）
        ("octocat/.github", "octocat", ".github"),
        ("a/.hidden", "a", ".hidden"),
        (
            "https://github.com/JoyWengXXX/.Net-microservices-API-template-ropo-",
            "JoyWengXXX",
            ".Net-microservices-API-template-ropo-",
        ),
        ("octocat/trailing-dash-", "octocat", "trailing-dash-"),
    ],
)
def test_valid_references_are_normalized(raw: str, owner: str, name: str) -> None:
    assert parse_github_repo(raw) == GitHubRepoRef(owner=owner, name=name)


@pytest.mark.parametrize(
    "raw",
    [
        # 空與缺段
        "",
        "octocat",
        "octocat/",
        "/Hello-World",
        "/",
        "//",
        # 路徑穿越
        "../x",
        "a/../b",
        "a/..",
        "a/.",
        "./a",
        "a/b/../c",
        "..%2f..%2fetc",
        "%2e%2e/x",
        "a/%2e%2e",
        # 以 - 開頭（會被當成選項）
        "-x/y",
        "--upload-pack=evil/y",
        "x/-y",
        "--help",
        # 以 . 結尾的名稱（以 . 開頭是合法的，見上方有效案例）
        "a/name.",
        "a/..",
        "a/...",
        # `.git` 結尾（短格式不剝除）
        "octocat/Hello-World.git",
        "octocat/Hello-World.GIT",
        "a/.git",
        "https://github.com/a/b.git.git",
        "https://github.com/a/.git",
        # 命令注入與特殊字元
        "a/b;rm",
        "a/b;rm -rf /",
        "a/b|c",
        "a/b&c",
        "a/b$(id)",
        "a/b`id`",
        "a/b'c",
        'a/b"c',
        "a/b\\c",
        "a\\b/c",
        "a/b*",
        "a/b?x=1",
        "a/b#frag",
        "a/b@c",
        "a b/c",
        "a/b c",
        # 空白與控制字元
        " octocat/Hello-World",
        "octocat/Hello-World ",
        "octocat/Hello-World\n",
        "octocat/Hello-World\r\n",
        "octocat\n/Hello-World",
        "octocat/Hello\tWorld",
        "octocat/Hello\x00World",
        "octocat/Hello\x1bWorld",
        "octocat/Hello\x7fWorld",
        "\u3000octocat/Hello-World",
        # 非 ASCII
        "octocat/你好",
        "ｏctocat/Hello",
        "octocat/Hellö",
        # owner 規則
        "-octocat/x",
        "a_b/x",
        "a.b/x",
        "o" * 40 + "/x",
        # name 長度（101 字元，見下方超長輸入區段的 name-101-chars）
        # 非 github.com 主機與協定
        "https://gitlab.com/octocat/Hello-World",
        "https://github.com.evil.com/octocat/Hello-World",
        "https://evil.com/github.com/octocat/Hello-World",
        "https://www.github.com/octocat/Hello-World",
        "https://api.github.com/octocat/Hello-World",
        "https://github.com:443/octocat/Hello-World",
        "https://github.com@evil.com/octocat/Hello-World",
        "http://github.com/octocat/Hello-World",
        "ftp://github.com/octocat/Hello-World",
        "file:///etc/passwd",
        "git://github.com/octocat/Hello-World",
        "ssh://git@github.com/octocat/Hello-World.git",
        "git@github.com:octocat/Hello-World.git",
        "github.com/octocat/Hello-World",
        "//github.com/octocat/Hello-World",
        # 帶帳密
        "https://user@github.com/octocat/Hello-World",
        "https://user:pass@github.com/octocat/Hello-World",
        "https://x-access-token:SECRET@github.com/octocat/Hello-World",
        # query、fragment、多餘路徑段
        "https://github.com/octocat/Hello-World?tab=readme",
        "https://github.com/octocat/Hello-World#readme",
        "https://github.com/octocat/Hello-World/tree/main",
        "https://github.com/octocat/Hello-World//",
        "https://github.com/octocat/Hello-World.git/info/refs",
        "https://github.com/octocat",
        "https://github.com/",
        "https://github.com",
        "octocat/Hello-World/extra",
        "octocat//Hello-World",
        # Windows 保留名稱
        "con/x",
        "x/CON",
        "x/nul.txt",
        "x/com1",
        "LPT9/x",
        # 超長輸入（以 id 命名，避免 pytest 把超長字串放進環境變數）
        pytest.param("a/" + "b" * 100_000, id="very-long-name"),
        pytest.param("x" * 257, id="over-length-limit"),
        pytest.param("o/" + "n" * 101, id="name-101-chars"),
    ],
)
def test_malicious_or_malformed_references_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidGitHubRepoError) as exc_info:
        parse_github_repo(raw)

    assert exc_info.value.code == "invalid_github_repo"


def test_error_message_does_not_echo_the_input() -> None:
    secret_like = "https://x-access-token:SENTINEL-SECRET@github.com/a/b"

    with pytest.raises(InvalidGitHubRepoError) as exc_info:
        parse_github_repo(secret_like)
    with pytest.raises(InvalidGitHubRepoError) as long_exc:
        parse_github_repo("SENTINEL-" + "x" * 5000)

    assert "SENTINEL" not in exc_info.value.message
    assert "SENTINEL" not in long_exc.value.message


def test_non_string_input_is_rejected() -> None:
    with pytest.raises(InvalidGitHubRepoError):
        parse_github_repo(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("owner", "name", "expected"),
    [
        ("octocat", "Hello-World", True),
        ("octocat", "..", False),
        ("octocat", "a.git", False),
        ("-bad", "x", False),
        ("ok", "a/b", False),
        ("ok", "", False),
    ],
)
def test_is_valid_owner_and_name_for_api_responses(owner: str, name: str, expected: bool) -> None:
    assert is_valid_owner_and_name(owner, name) is expected
