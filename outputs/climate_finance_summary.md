# Climate finance database summary

## Coverage
- OECD CRS project-level commitments for year(s): [2024, 2023]
- World Bank project-level climate coefficients dataset: https://devinit.github.io/media/documents/WB_with_climate_coefficients.xlsx
- IDB direct climate datasets: https://data.iadb.org/file/download/5f463558-6c7f-41ac-9ab9-6b08bc0e3df5, https://data.iadb.org/file/download/bc40fafc-4241-4d9a-afe6-e28a56a17697
- AIIB direct project list feed: https://www.aiib.org/en/projects/list/.content/all-projects-data.js
- CDB climate loan disclosures (PDF): ['https://www.cdb.com.cn/xwzx/xxgg/qtgg/202407/W020240729639508752090.pdf', 'https://www.cdb.com.cn/xwzx/xxgg/qtgg/202501/W020250113626252031243.pdf', 'https://www.cdb.cn/xwzx/xxgg/qtgg/202506/W020250618525732514513.pdf']

## Totals
- Total climate finance represented: **339,121.9 USD million**
- Number of project rows: **64,965**

## Climate amount by source dataset
- AIIB_DIRECT_PROJECT_LIST: 61,223.4 USD million
- CDB_QUARTERLY_CLIMATE_DISCLOSURE: 23,828.3 USD million
- IDB_CLIMATE_DATA_DIRECT: 11,661.9 USD million
- OECD_CRS: 91,642.5 USD million
- WORLD_BANK_PROJECT_CLIMATE_COEFFICIENTS: 150,765.9 USD million

## Climate amount by objective
- adaptation: 53,184.0 USD million
- both: 114,604.5 USD million
- mitigation: 171,333.4 USD million

## Top actors by climate amount
- World Bank (IBRD/IDA): 150,765.9 USD million
- Asian Infrastructure Investment Bank: 61,223.4 USD million
- China Development Bank: 23,828.3 USD million
- Japan / Japanese International Co-operation Agency: 17,924.6 USD million
- Inter-American Development Bank: 11,661.9 USD million
- France / French Development Agency: 8,851.4 USD million
- EU Institutions / European Commission: 8,641.6 USD million
- Germany / KfW Bankengruppe (KfW banking group): 6,253.5 USD million
- Germany / Federal Ministry for Economic Cooperation and Development: 5,257.7 USD million
- United States / Agency for International Development: 4,734.2 USD million

## Top sectors by climate amount
- energy: 99,003.2 USD million
- transport: 88,296.5 USD million
- other: 46,018.9 USD million
- water: 38,479.8 USD million
- finance_policy: 24,509.9 USD million
- agriculture_land_use: 19,343.9 USD million
- health: 7,317.5 USD million
- industry_mining: 5,713.6 USD million
- buildings: 5,426.2 USD million
- education: 4,368.1 USD million

## Method notes
- OECD climate amounts are estimated from Rio markers using coefficients: principal=100%, significant=40%.
- AIIB objective tags are inferred from project title/sector keywords due to no explicit adaptation/mitigation split in the source feed.
- CDB data is currently aggregate climate-loan disclosure rows (quarterly), not fully project-level line items.
- Objective classification is mutually exclusive (`mitigation`, `adaptation`, `both`) for flow visualisation.
- Instrument classes are normalized to `grant`, `debt`, `equity`, `guarantee`, `other`.
- Sector classes are keyword-based harmonization across source taxonomies.