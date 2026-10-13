"""
monitor.py - avisa no Discord quando sai vídeo novo no YouTube e no Instagram.

- YouTube:   feed RSS público do canal (sem chave de API e sem navegador).
- Instagram: Playwright lendo a página pública do perfil, deslogado.

Não existe nenhuma forma de login: o navegador abre em modo anônimo e não
guarda cookies nem sessão entre as checagens.

Uso como módulo (pelo bot):
    monitor = ProfileMonitor(config["monitor"], notify=funcao_async_que_envia_texto)
    await monitor.check_all()

Uso pela linha de comando:
    python monitor.py testar             # lê os perfis e mostra o que achou (não avisa nada)
    python monitor.py testar visivel     # igual, mas com a janela do navegador aberta
"""

import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright


STATE_FILE = Path("monitor_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

MAX_SEEN = 500              # quantos IDs já vistos ficam guardados por plataforma
MAX_NEW_AT_ONCE = 10        # acima disso, não anuncia um por um (provável leitura estranha)
FAILURES_BEFORE_ALERT = 3   # falhas seguidas até avisar no Discord
FETCH_TIMEOUT = 120         # segundos por plataforma

PLATFORMS = ("youtube", "instagram")
LABELS = {"youtube": "YouTube", "instagram": "Instagram"}
EMOJIS = {"youtube": "▶️", "instagram": "📸"}

ATOM = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class FetchError(Exception):
    """Falha esperada ao ler um perfil (captcha, login, layout mudou...)."""


@dataclass
class Post:
    id: str
    url: str
    title: str = ""


async def _block_heavy_resources(route):
    # Só os links dos posts importam: não baixa imagens, vídeos nem fontes.
    # Isso reduz muito o número de requisições de cada leitura da página.
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


def describe_error(error):
    if isinstance(error, FetchError):
        return str(error)
    if isinstance(error, asyncio.TimeoutError):
        return "tempo esgotado"
    return f"{type(error).__name__}: {error}"[:300]


# ----------------------------------------------------------------------
# Leitura de cada plataforma
# ----------------------------------------------------------------------

def parse_youtube_feed(xml_text):
    root = ET.fromstring(xml_text)
    posts = []

    for entry in root.findall("a:entry", ATOM):
        video_id = entry.findtext("yt:videoId", namespaces=ATOM)

        if not video_id:
            continue

        title = (entry.findtext("a:title", namespaces=ATOM) or "").strip()
        link = entry.find("a:link", ATOM)

        if link is not None and link.get("href"):
            url = link.get("href")
        else:
            url = f"https://www.youtube.com/watch?v={video_id}"

        posts.append(Post(video_id, url, title))

    return posts


def instagram_extractor():
    pattern = re.compile(r"/(p|reel)/([\w-]+)")

    def extract(href):
        found = pattern.search(href)

        if not found:
            return None

        kind, code = found.group(1), found.group(2)

        return Post(code, f"https://www.instagram.com/{kind}/{code}/")

    return extract


# ----------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------

class ProfileMonitor:
    def __init__(self, config, notify):
        """
        config: dict com "profiles" (youtube/instagram), e opcionalmente
                "headless" (padrão True) e "youtube_channel_id".
        notify: função async que recebe um texto e envia ao Discord.
        """
        self.profiles = config.get("profiles", {})
        self.headless = config.get("headless", True)
        self.youtube_channel_id = config.get("youtube_channel_id") or None
        self.notify = notify

        self.state = self._load_state()
        self.failures = {}
        self.alerted = set()
        self._locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self.last_new = {}   # plataforma -> quando anunciou um vídeo novo (epoch)

    def needs_seed(self):
        """True se algum perfil ainda não teve a primeira leitura (sem estado salvo)."""
        return any(
            self.profiles.get(platform)
            and "seen" not in self.state.get(platform, {})
            for platform in PLATFORMS
        )

    # ---------- estado ----------

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                print("[MONITOR] monitor_state.json ilegível, começando do zero")

        return {}

    def _save_state(self):
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        tmp.replace(STATE_FILE)

    # ---------- leitura ----------

    async def fetch(self, platform, url):
        if platform == "youtube":
            return await self._fetch_youtube(url)

        if platform == "instagram":
            return await self._scrape(platform, url, instagram_extractor())

        raise FetchError(f"plataforma desconhecida: {platform}")

    async def _fetch_youtube(self, url):
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "pt-BR,pt;q=0.9",
        }

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers
        ) as session:
            channel_id = (
                self.youtube_channel_id
                or self.state.get("youtube", {}).get("channel_id")
            )

            if not channel_id:
                async with session.get(url) as response:
                    html = await response.text()

                for pattern in (
                    r"youtube\.com/channel/(UC[\w-]{22})",
                    r'"channelId":"(UC[\w-]{22})"',
                    r'"externalId":"(UC[\w-]{22})"',
                ):
                    match = re.search(pattern, html)

                    if match:
                        channel_id = match.group(1)
                        break

                if not channel_id:
                    raise FetchError(
                        "não consegui descobrir o ID do canal do YouTube "
                        "(coloque \"youtube_channel_id\" no config.json)"
                    )

                self.state.setdefault("youtube", {})["channel_id"] = channel_id
                self._save_state()

            feed_url = (
                "https://www.youtube.com/feeds/videos.xml"
                f"?channel_id={channel_id}"
            )

            async with session.get(feed_url) as response:
                if response.status != 200:
                    raise FetchError(
                        f"o feed do YouTube respondeu HTTP {response.status}"
                    )

                xml_text = await response.text()

        posts = parse_youtube_feed(xml_text)

        if not posts:
            raise FetchError("o feed do YouTube veio vazio")

        return posts

    async def _scrape(self, platform, url, extract):
        async with async_playwright() as pw:
            # Navegador anônimo e descartável: nada de login, cookies ou
            # sessão guardados entre as checagens.
            browser = await pw.chromium.launch(headless=self.headless)

            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                    viewport={"width": 1280, "height": 900},
                )

                await context.route("**/*", _block_heavy_resources)

                page = await context.new_page()

                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45_000
                )

                posts = {}

                for _ in range(3):
                    await page.wait_for_timeout(4_000)

                    if re.search(r"/(accounts/)?login", page.url):
                        raise FetchError("a página redirecionou para o login")

                    hrefs = await page.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => e.getAttribute('href'))"
                    )

                    for href in hrefs:
                        post = extract(href or "")

                        if post and post.id not in posts:
                            posts[post.id] = post

                    if posts:
                        break

                    await page.mouse.wheel(0, 1500)

                if not posts:
                    raise FetchError(await self._explain_empty(page))

                return list(posts.values())

            finally:
                await browser.close()

    async def _explain_empty(self, page):
        try:
            body = (await page.inner_text("body"))[:3000].lower()
        except Exception:
            body = ""

        if any(word in body for word in ("captcha", "verify", "verificar", "puzzle")):
            return "a página pediu verificação (captcha)"

        if any(word in body for word in ("log in", "login", "entrar")):
            return "a página pediu login"

        return (
            "nenhum vídeo encontrado na página "
            "(o layout pode ter mudado ou o perfil está sem posts)"
        )

    # ---------- processamento ----------

    def detected_since(self, platform, timestamp):
        """True se já anunciou um vídeo novo desse perfil depois de `timestamp`."""
        return self.last_new.get(platform, 0) >= timestamp

    async def check_all(self, platforms=None):
        """
        Confere os perfis (todos, ou só os de `platforms`).
        Retorna {plataforma: texto de status}.
        """
        selected = [
            platform
            for platform in PLATFORMS
            if self.profiles.get(platform)
            and (platforms is None or platform in platforms)
        ]

        statuses = await asyncio.gather(
            *(self._check_platform(platform) for platform in selected)
        )

        return dict(zip(selected, statuses))

    async def _check_platform(self, platform):
        url = self.profiles[platform]

        # Uma trava por plataforma: o YouTube (rápido) nunca espera o
        # Instagram (navegador), mas duas leituras do mesmo perfil não se misturam.
        async with self._locks[platform]:
            try:
                posts = await asyncio.wait_for(
                    self.fetch(platform, url),
                    timeout=FETCH_TIMEOUT
                )
            except Exception as error:
                await self._register_failure(platform, error)
                return f"❌ {describe_error(error)}"

            await self._register_success(platform)
            new_count = await self._process(platform, posts)

            if new_count:
                self.last_new[platform] = time.time()

        if new_count is None:
            return f"✅ primeira leitura, {len(posts)} itens memorizados"

        return f"✅ {len(posts)} itens lidos, {new_count} novo(s)"

    async def _process(self, platform, posts):
        """Retorna None na primeira leitura, ou a quantidade de itens novos."""
        label = LABELS[platform]
        platform_state = self.state.setdefault(platform, {})
        seen = platform_state.get("seen")

        # Primeira leitura: só memoriza o que já existe, sem avisar nada.
        if seen is None:
            ids = [post.id for post in reversed(posts)]
            platform_state["seen"] = ids[-MAX_SEEN:]
            self._save_state()
            print(f"[MONITOR] {label}: primeira leitura, {len(posts)} itens memorizados")
            return None

        seen_set = set(seen)
        new_posts = [post for post in posts if post.id not in seen_set]

        if not new_posts:
            return 0

        # Memoriza antes de avisar, para nunca avisar o mesmo vídeo duas vezes.
        oldest_first = list(reversed(new_posts))
        platform_state["seen"] = (
            seen + [post.id for post in oldest_first]
        )[-MAX_SEEN:]
        self._save_state()

        if len(new_posts) > MAX_NEW_AT_ONCE:
            await self.notify(
                f"⚠️ **{label}**: apareceram {len(new_posts)} itens novos de uma vez. "
                "Não anunciei um por um porque pode ser uma leitura estranha da página. "
                "Vale conferir o perfil."
            )
            return len(new_posts)

        for post in oldest_first:
            await self.notify(self._format(platform, post))
            print(f"[MONITOR] {label}: novo -> {post.url}")

        return len(new_posts)

    def _format(self, platform, post):
        lines = [f"{EMOJIS[platform]} **Vídeo postado no {LABELS[platform]}!**"]

        if post.title:
            lines.append(f"🎬 {post.title}")

        lines.append(f"🔗 {post.url}")
        lines.append(f"🕒 Detectado <t:{int(time.time())}:R>")

        return "\n".join(lines)

    # ---------- falhas ----------

    async def _register_failure(self, platform, error):
        label = LABELS[platform]
        count = self.failures.get(platform, 0) + 1
        self.failures[platform] = count

        print(f"[MONITOR] {label}: falha {count} ({describe_error(error)})")

        if count >= FAILURES_BEFORE_ALERT and platform not in self.alerted:
            self.alerted.add(platform)
            await self.notify(
                f"⚠️ **Monitor do {label}** falhou {count} vezes seguidas: "
                f"{describe_error(error)}\n"
                "Os avisos desse perfil ficam parados até voltar."
            )

    async def _register_success(self, platform):
        self.failures[platform] = 0

        if platform in self.alerted:
            self.alerted.discard(platform)
            await self.notify(
                f"✅ Monitor do {LABELS[platform]} voltou a funcionar."
            )


# ----------------------------------------------------------------------
# Linha de comando: testar a leitura dos perfis
# ----------------------------------------------------------------------

async def _print_notify(text):
    print(text)


async def _testar(visivel):
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    monitor_config = dict(config["monitor"])

    if visivel:
        monitor_config["headless"] = False

    monitor = ProfileMonitor(monitor_config, notify=_print_notify)

    for platform in PLATFORMS:
        url = monitor.profiles.get(platform)

        if not url:
            continue

        print(f"\n== {LABELS[platform]} ({url}) ==")

        try:
            posts = await monitor.fetch(platform, url)
        except Exception as error:
            print(f"FALHOU: {describe_error(error)}")
            continue

        print(f"{len(posts)} itens encontrados. Primeiros:")

        for post in posts[:5]:
            print(f" - {post.url} {post.title}")


if __name__ == "__main__":
    args = sys.argv[1:]

    if args[:1] == ["testar"]:
        asyncio.run(_testar(visivel="visivel" in args[1:]))
    else:
        print(__doc__)