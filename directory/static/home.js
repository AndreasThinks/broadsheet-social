// Keeps the front-page app picture ticking over: a "1 new post" pill, then a post from the queue slides onto the top; likes
// count up; a post that is also on a second network folds into one card. Still for anyone who turns motion off.
(function () {
  const pic = document.querySelector(".app-picture");
  if (!pic || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const feed = pic.querySelector(".pic-feed"), pill = pic.querySelector(".pic-pill");
  const queue = [...pic.querySelector(".pic-queue").content.children];
  const icons = {};
  pic.querySelectorAll(".pic-badge").forEach(b => { icons[b.classList[1]] = b.innerHTML; });
  queue.forEach(s => s.querySelectorAll(".pic-badge").forEach(b => { icons[b.classList[1]] = b.innerHTML; }));
  const names = { bluesky: "Bluesky", mastodon: "Mastodon", x: "X" };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let visible = false, i = 0;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe(pic);
  const awake = () => visible && !document.hidden;

  function fold(slot) {
    const nets = slot.dataset.nets.split(" "), post = slot.querySelector(".pic-post");
    nets.slice(1).forEach(k => {
      const b = document.createElement("span");
      b.className = `pic-badge ${k} pop`; b.innerHTML = icons[k] || "";
      slot.querySelector(".pic-badges").append(b);
    });
    slot.querySelector(".pic-nets").innerHTML = nets.map(k => `<span class="net-${k}">${names[k]}</span>`).join(" + ");
    slot.querySelector(".pic-also").hidden = false;
    post.classList.add("flash");
    requestAnimationFrame(() => setTimeout(() => post.classList.remove("flash"), 60));
  }

  setInterval(async () => {
    if (!awake()) return;
    const slot = queue[i++ % queue.length].cloneNode(true);
    pill.replaceChildren(slot.querySelector(".pic-face").cloneNode(true), "1 new post");
    pill.classList.add("show");
    await sleep(1000);
    pill.classList.remove("show");
    feed.prepend(slot);
    slot.animate([{ height: "0px", opacity: 0 }, { height: slot.scrollHeight + "px", opacity: 1 }],
                 { duration: 600, easing: "cubic-bezier(.2,.7,.2,1)" });
    while (feed.children.length > 5) feed.lastElementChild.remove();
    if (slot.dataset.nets) { await sleep(1700); fold(slot); }
  }, 4600);

  setInterval(() => {
    if (!awake()) return;
    const likes = [...feed.querySelectorAll(".pic-likes")].slice(0, 3);
    if (!likes.length) return;
    const b = likes[Math.floor(Math.random() * likes.length)];
    b.textContent = +b.textContent + 1;
    b.classList.remove("bump"); void b.offsetWidth; b.classList.add("bump");
  }, 1900);
})();
