"""One-shot recon: open target channel, click a video, dump media viewer DOM."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

USER_DATA_DIR = r"D:\UserData\.playwright_user_data--claude"
TARGET_HASH = "#-1001395144198"
OUT = Path(__file__).parent / "resources" / "probe_media_viewer.html"


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            channel="msedge",
            accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            link = page.locator(f"a[href='{TARGET_HASH}']").first
            await link.click()
            await page.wait_for_timeout(3000)
            # Click first .media-inner.interactive (video)
            video = page.locator(".Transition_slide-active .media-inner.interactive").first
            await video.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
            await video.click()
            await page.wait_for_timeout(3000)
            # Dump body innerHTML restricted to non-message-list parts
            html = await page.evaluate("""() => {
                const viewer = document.querySelector('.MediaViewer, .MediaViewerSlides, [class*="MediaViewer"], [class*="modal"], [class*="Modal"]');
                if (viewer) return viewer.outerHTML;
                const overlays = Array.from(document.body.children).filter(c => {
                    const cn = c.className;
                    return typeof cn === 'string' && (cn.includes('Viewer') || cn.includes('Modal') || cn.includes('overlay'));
                });
                return overlays.map(o => o.outerHTML).join('\\n\\n=========\\n\\n');
            }""")
            OUT.write_text(html or "(empty)", encoding="utf-8")
            print(f"saved {OUT} len={len(html or '')}")
            # Also dump aria-labels of buttons in viewer for download button discovery
            buttons = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('button, [role=button]')).filter(b => b.offsetParent !== null).map(b => ({
                    aria: b.getAttribute('aria-label'),
                    title: b.getAttribute('title'),
                    cls: b.className,
                    text: (b.innerText || '').slice(0, 40)
                })).slice(0, 80);
            }""")
            (OUT.parent / "probe_viewer_buttons.json").write_text(__import__('json').dumps(buttons, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"saved {OUT.parent}/probe_viewer_buttons.json count={len(buttons)}")
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
