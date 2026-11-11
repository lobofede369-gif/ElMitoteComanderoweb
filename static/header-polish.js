/* Evita duplicar el nombre y el rol cuando ambos valores son iguales. */
(() => {
  const node = document.getElementById('role-name');
  if (!node) return;
  const clean = () => {
    const value = node.textContent.trim();
    const parts = value.split(/\s*[·•]\s*/u);
    if (parts.length === 2 && parts[0] && parts[1] &&
        parts[0].toLocaleLowerCase('es-MX') === parts[1].toLocaleLowerCase('es-MX')) {
      node.textContent = parts[0];
    }
  };
  new MutationObserver(clean).observe(node, {
    childList: true, characterData: true, subtree: true
  });
  clean();
})();
