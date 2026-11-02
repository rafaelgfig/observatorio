// @ts-check
/**
 * Mapa único (MapLibre GL) compartilhado pelas views.
 *
 * Basemap vetorial do OpenFreeMap (sem chave de API): estilo "dark" no tema escuro e
 * "positron" no claro. Trocar de tema recria o estilo — por isso as views registram suas
 * camadas com `onStyle(fn)`, que roda agora e a cada novo estilo carregado.
 */

import * as maplibregl from "maplibre-gl";
import { currentTheme } from "./theme.js";

export const BASEMAPS = {
  dark: "https://tiles.openfreemap.org/styles/dark",
  light: "https://tiles.openfreemap.org/styles/positron",
};

/** @type {[[number, number], [number, number]]} */
export const BRASIL_BOUNDS = [
  [-74.1, -33.8],
  [-34.7, 5.4],
];

/**
 * Caixa da cidade de São Paulo: a mesma de `itbi.queries.SP_EXTENT` no servidor (recorte dos
 * tiles), conferida por `tests/test_frontend.py`.
 * @type {[[number, number], [number, number]]}
 */
export const SAO_PAULO_BOUNDS = [
  [-46.83, -24.01],
  [-46.36, -23.35],
];

/**
 * Tipos do estilo do MapLibre usados no JSDoc das views (o pacote os reexporta da style-spec).
 * @typedef {maplibregl.ExpressionSpecification} ExpressionSpecification
 * @typedef {maplibregl.FillLayerSpecification} FillLayerSpecification
 * @typedef {maplibregl.LineLayerSpecification} LineLayerSpecification
 * @typedef {maplibregl.CircleLayerSpecification} CircleLayerSpecification
 * @typedef {maplibregl.SymbolLayerSpecification} SymbolLayerSpecification
 */

/**
 * @typedef {object} MapContext
 * @property {maplibregl.Map} map
 * @property {typeof maplibregl} maplibregl
 * @property {(fn: () => void) => () => void} onStyle  registra camadas; devolve função p/ cancelar
 * @property {(theme: "dark" | "light") => void} setTheme
 * @property {() => string | undefined} labelLayerId   rótulos do basemap (inserir camadas de dados abaixo)
 * @property {(bbox: [number, number, number, number] | [[number, number], [number, number]], maxZoom?: number, minZoom?: number) => void} fit
 *   enquadra a caixa sem passar de `maxZoom` nem ficar abaixo de `minZoom` (centraliza nela)
 */

/**
 * @param {HTMLElement} container
 * @returns {MapContext}
 */
export function createMap(container) {
  const map = new maplibregl.Map({
    container,
    style: BASEMAPS[currentTheme()],
    center: [-52, -15],
    zoom: 3,
    attributionControl: { compact: true },
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
    maxPitch: 0,
    cooperativeGestures: false,
    // rótulos dos controles em português (a página é pt-BR)
    locale: {
      "NavigationControl.ZoomIn": "Aproximar",
      "NavigationControl.ZoomOut": "Afastar",
      "NavigationControl.ResetBearing": "Restaurar orientação",
      "AttributionControl.ToggleAttribution": "Mostrar/ocultar atribuição",
      "AttributionControl.MapFeedback": "Comentários sobre o mapa",
      "ScaleControl.Meters": "m",
      "ScaleControl.Kilometers": "km",
    },
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

  /** @type {Set<() => void>} */
  const callbacks = new Set();
  let styleReady = false;

  const runAll = () => {
    for (const fn of callbacks) {
      try {
        fn();
      } catch (error) {
        console.error("onStyle:", error);
      }
    }
  };

  /** Camadas do basemap (as das views são acrescentadas depois). */
  let baseLayers = new Set();
  map.on("style.load", () => {
    baseLayers = new Set(map.getStyle().layers.map((layer) => layer.id));
    styleReady = true;
    runAll();
  });
  // O contêiner pode ainda não ter tamanho na construção; enquadra quando o mapa carregar
  // (a menos que uma view já tenha movido a câmera).
  let userMoved = false;
  map.once("movestart", () => {
    userMoved = true;
  });
  map.once("load", () => {
    if (!userMoved) map.fitBounds(BRASIL_BOUNDS, { padding: 24, animate: false });
  });

  return {
    map,
    maplibregl,
    onStyle(fn) {
      callbacks.add(fn);
      if (styleReady) fn();
      return () => callbacks.delete(fn);
    },
    setTheme(theme) {
      styleReady = false;
      map.setStyle(BASEMAPS[theme]);
    },
    labelLayerId() {
      // 1ª camada de rótulos acima de todas as linhas/preenchimentos do basemap: dados ficam
      // sobre ruas e prédios, e os nomes continuam legíveis por cima deles.
      const base = map.getStyle().layers.filter((layer) => baseLayers.has(layer.id));
      let last = -1;
      base.forEach((layer, i) => {
        if (layer.type === "line" || layer.type === "fill" || layer.type === "fill-extrusion") last = i;
      });
      return base.slice(last + 1).find((layer) => layer.type === "symbol")?.id;
    },
    fit(bbox, maxZoom = 15, minZoom = 0) {
      /** @type {[[number, number], [number, number]]} */
      const bounds =
        bbox.length === 4
          ? [
              [bbox[0], bbox[1]],
              [bbox[2], bbox[3]],
            ]
          : bbox;
      const camera = minZoom > 0 ? map.cameraForBounds(bounds, { padding: 48, maxZoom }) : undefined;
      if (camera?.center && camera.zoom !== undefined && camera.zoom < minZoom) {
        // mapa pequeno (celular, notebook baixo): a caixa inteira não cabe sem sair do zoom útil
        map.easeTo({ center: camera.center, zoom: minZoom, duration: 700 });
        return;
      }
      map.fitBounds(bounds, { padding: 48, maxZoom, duration: 700 });
    },
  };
}
