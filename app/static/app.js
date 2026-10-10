// Call page: click a transcript turn to play from there; highlight the turn being played.
document.addEventListener("DOMContentLoaded", () => {
  const player = document.getElementById("player");
  if (!player) return;
  const turns = Array.from(document.querySelectorAll(".turn[data-start]"));

  turns.forEach((turn) => {
    turn.addEventListener("click", () => {
      player.currentTime = parseFloat(turn.dataset.start);
      player.play();
    });
  });

  player.addEventListener("timeupdate", () => {
    let current = null;
    for (const turn of turns) {
      if (parseFloat(turn.dataset.start) <= player.currentTime + 0.05) current = turn;
    }
    turns.forEach((turn) => turn.classList.toggle("playing", turn === current));
  });

  // Evidence links in the grade card jump to their turn and play it.
  document.querySelectorAll("[data-seek-turn]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const turn = document.getElementById(`turn-${link.dataset.seekTurn}`);
      if (!turn) return;
      turn.scrollIntoView({ behavior: "smooth", block: "center" });
      turn.click();
    });
  });
});
