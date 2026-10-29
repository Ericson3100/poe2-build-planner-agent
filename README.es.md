# Planificador de builds de Path of Exile 2

*English version: [README.md](README.md).*

Proyecto final de Procesamiento de Lenguaje Natural (Maestría en IA, USFQ). Un agente ReAct con Claude compara tu
personaje con una build guía y te dice qué comprar, qué pasivos cambiar, qué skills apagar y qué ascendencia tomar
según tu presupuesto. Cada recomendación se mide con el motor de Path of Building antes de entrar al plan.

Todo el proyecto está en `proyecto_final.ipynb`: el código (en celdas `%%writefile`), el experimento, la
evaluación y las conclusiones.

## Cómo ejecutarlo

Requiere Windows: el motor de Path of Building se carga desde sus librerías compiladas para Windows. Las alternativas para Linux y macOS están al inicio del notebook.

1. Crea un archivo `.env` junto al notebook con tu clave: `ANTHROPIC_API_KEY=...` (hay una plantilla en
   `.env.example`). Sin clave el notebook corre igual, pero solo con la línea base.
2. Abre `proyecto_final.ipynb` y ejecuta todas las celdas. La primera vez clona Path of Building en `vendor/`
   (unos 750 MB).
3. Los parámetros están en la sección 5 (`BUILD`, `GUIDE`, `OBJECTIVE`, `RESIST_FLOOR`).

## Datos incluidos

- `builds/`: el personaje exportado de Path of Building. Para usar otro, guarda su XML aquí.
- `guides/`: las guías, una carpeta por guía (variantes `.build` y URL de origen; los textos de las guías no se redistribuyen, ver `guides/README.md`). Se pueden agregar más con `GUIDE_SOURCES`.
- `wallet.example.toml`: plantilla del oro y la currency del jugador; el notebook la copia a `wallet.toml` (no se versiona).
- `data/`: el árbol de pasivos y el índice del RAG ya construido (93 páginas de poe2wiki).
- `reports/`: los reportes y métricas de las corridas finales, y los resultados de RAGAS. Volver a ejecutar la
  evaluación de RAGAS muestra estos resultados sin gastar.

## Fuentes externas

Path of Building Community PoE2 (MIT), el trade site oficial de PoE2, poe.ninja, poe2wiki.net (CC BY-NC-SA 3.0) y
el modelo de embeddings `BAAI/bge-small-en-v1.5` (MIT). Los detalles están en la sección 2 del notebook.
