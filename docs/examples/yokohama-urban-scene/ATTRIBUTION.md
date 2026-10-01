# Attribution and licensing

Source: Yokohama City / Project PLATEAU, **3D City Model of Yokohama, catalog fiscal year 2024**.

- [Official dataset](https://www.geospatial.jp/ckan/dataset/plateau-14100-yokohama-shi-2024)
- [PLATEAU site policy](https://www.mlit.go.jp/plateau/site-policy/)
- Selected source-data license: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).

MissionOS selected a 600 m square neighborhood, transformed horizontal coordinates, triangulated source surfaces, added uniform display materials and generated conservative collision proxies. The route, decision locations and delivery pad were added by MissionOS. These modifications are not municipal or MLIT-authored plans.

The geometry in `scene.json`, GLB, OBJ, footprint GeoJSON and its embedded HTML/screenshot representations is derived from the attributed source under CC BY 4.0. This data license takes precedence over the repository's default code license for these data assets. New builder/viewer code follows the repository's code license. Three.js r160 is MIT licensed; its complete notice is in [vendor/THREE-LICENSE.txt](vendor/THREE-LICENSE.txt) and the embedded library retains its notice.

The catalog/ZIP filename says 2024, while the bundled source README says 2025年度, created 2025-10-28. Both facts are retained in [source-manifest.json](source-manifest.json). Source geometry does not establish current real-world conditions.

出典：横浜市・Project PLATEAU「3D都市モデル（横浜市、公開カタログ2024年度版）」を加工して作成。街区抽出・座標変換・三角形化・表示および衝突用形状への加工：MissionOS。飛行経路・判断地点・配送スペースは検証用の追加です。
