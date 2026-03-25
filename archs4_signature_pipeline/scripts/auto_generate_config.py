#!/usr/bin/env python3
"""
auto_generate_config.py - Auto-generate ARCHS4 pipeline configs from disease list

Automatically fetches disease synonyms from OpenTargets API so that
ARCHS4 sample search uses the best possible keywords without manual
configuration for each new disease.

Usage:
  python scripts/auto_generate_config.py --disease-list ../../ops/internal/disease_list_day1_dual.txt
  python scripts/auto_generate_config.py --disease nash --disease-name "nonalcoholic steatohepatitis" --efo-id EFO_1001249
"""
import argparse
import logging
import re
from pathlib import Path
from urllib.request import Request, urlopen
import json

import yaml

logger = logging.getLogger("archs4.config")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Extra keywords not in ontology (abbreviations, colloquial terms)
# Extra keywords not found in ontology — only use terms >= 3 chars
# that are unambiguous enough to avoid false matches in sample metadata.
EXTRA_KEYWORDS = {
    # --- Metabolic / Liver ---
    "nash": ["NASH", "MASH", "NAFLD", "fatty liver", "hepatic steatosis", "steatosis"],
    "nafld": ["NAFLD", "MASLD", "NASH", "fatty liver", "hepatic steatosis", "steatosis"],
    "metabolic_syndrome": ["MetS", "insulin resistance", "obesity", "hyperglycemia"],
    # --- Autoimmune / Inflammatory ---
    "lupus": ["SLE", "lupus nephritis", "lupus erythematosus"],
    "psoriasis": ["psoriatic", "psoriasis vulgaris", "plaque psoriasis"],
    "crohns_disease": ["Crohn", "Crohns", "ileitis", "inflammatory bowel"],
    "ankylosing_spondylitis": ["ankylosing", "spondylitis", "axial spondyloarthritis"],
    # --- Neurodegeneration ---
    "alzheimers_disease": ["Alzheimer", "Alzheimers", "amyloid", "tauopathy", "dementia"],
    "parkinsons_disease": ["Parkinson", "Parkinsons", "dopaminergic", "substantia nigra"],
    "als": ["ALS", "motor neuron disease", "motor neuron degeneration", "spinal cord"],
    "huntingtons_disease": ["Huntington", "Huntingtons", "huntingtin", "HTT", "polyglutamine"],
    # --- Fibrosis ---
    "ipf": ["IPF", "lung fibrosis", "pulmonary fibrosis", "interstitial lung disease"],
    "liver_fibrosis": ["liver fibrosis", "hepatic fibrosis", "cirrhosis", "fibrotic liver"],
    "renal_fibrosis": ["renal fibrosis", "kidney fibrosis", "nephrosclerosis", "CKD"],
    # --- Oncology ---
    "pancreatic_cancer": ["PDAC", "pancreatic adenocarcinoma", "pancreatic ductal", "pancreatic tumor"],
    "glioblastoma": ["GBM", "glioma", "brain tumor", "astrocytoma"],
    "triple_negative_breast_cancer": ["TNBC", "triple negative", "basal-like breast cancer"],
    "nsclc": ["NSCLC", "non-small cell lung cancer", "lung adenocarcinoma", "lung squamous cell carcinoma"],
    "melanoma": ["cutaneous melanoma", "malignant melanoma", "BRAF melanoma", "metastatic melanoma"],
    "prostate_cancer": ["prostate adenocarcinoma", "CRPC", "castration-resistant", "prostate tumor"],
    "hepatocellular_carcinoma": ["HCC", "liver cancer", "hepatoma", "hepatocellular"],
    "aml": ["AML", "acute myeloid leukemia", "myeloid leukemia", "myeloblastic leukemia"],
    "ovarian_cancer": ["HGSOC", "high-grade serous ovarian", "ovarian carcinoma", "ovarian tumor"],
    "renal_cell_carcinoma": ["ccRCC", "clear cell renal", "kidney cancer", "renal carcinoma"],
    "gastric_cancer": ["stomach cancer", "gastric adenocarcinoma", "gastric carcinoma", "stomach tumor"],
    "head_neck_cancer": ["HNSCC", "head and neck cancer", "oral squamous cell carcinoma", "oropharyngeal"],
    "multiple_myeloma": ["myeloma", "plasma cell neoplasm", "plasmacytoma", "MM"],
    "cholangiocarcinoma": ["bile duct cancer", "biliary tract cancer", "intrahepatic cholangiocarcinoma", "CCA"],
    # --- Respiratory ---
    "copd": ["COPD", "emphysema", "chronic bronchitis", "airflow obstruction",
             "chronic obstructive", "GOLD stage"],
    "asthma": ["bronchial asthma", "eosinophilic asthma", "airway hyperresponsiveness",
               "allergic asthma", "severe asthma"],
    # --- Autoimmune / Inflammatory (batch 2) ---
    "ulcerative_colitis": ["ulcerative colitis", "colitis", "inflammatory bowel",
                           "IBD", "proctitis", "pancolitis"],
    "systemic_sclerosis": ["scleroderma", "SSc", "systemic sclerosis", "skin fibrosis",
                           "Raynaud", "diffuse cutaneous"],
    "giant_cell_arteritis": ["GCA", "temporal arteritis", "large vessel vasculitis",
                             "giant cell", "cranial arteritis"],
    "eosinophilic_esophagitis": ["EoE", "eosinophilic esophagitis", "esophageal eosinophilia",
                                 "allergic esophagitis", "eosinophilic oesophagitis"],
    # --- Dermatology ---
    "atopic_dermatitis": ["eczema", "atopic eczema", "atopic dermatitis",
                          "skin inflammation", "allergic dermatitis"],
    "vitiligo": ["depigmentation", "melanocyte", "leukoderma", "repigmentation"],
    "alopecia_areata": ["alopecia areata", "alopecia", "hair loss",
                        "autoimmune alopecia", "patchy alopecia"],
    # --- Hematology / Oncology ---
    "myelofibrosis": ["primary myelofibrosis", "bone marrow fibrosis",
                      "myeloproliferative neoplasm", "post-polycythemia vera myelofibrosis"],
    "colorectal_cancer": ["colorectal carcinoma", "colon cancer", "rectal cancer",
                          "colorectal adenocarcinoma", "CRC"],
    # --- Kidney / Liver rare (batch 2) ---
    "chronic_kidney_disease": ["CKD", "chronic renal disease", "renal insufficiency",
                               "kidney disease", "end-stage renal", "ESRD"],
    "primary_biliary_cholangitis": ["PBC", "primary biliary cirrhosis", "biliary cholangitis",
                                    "cholestatic liver", "antimitochondrial"],
    "iga_nephropathy": ["IgA nephropathy", "Berger disease", "mesangial IgA",
                        "IgA glomerulonephritis", "IgAN"],
    # --- Existing cardiovascular (unchanged) ---
    "pulmonary_arterial_hypertension": ["PAH"],
    "myocardial_infarction": ["AMI", "STEMI", "NSTEMI"],
    "venous_thromboembolism": ["VTE"],
    "deep_vein_thrombosis": ["DVT"],
    "abdominal_aortic_aneurysm": ["AAA"],
    "heart_failure": ["HFrEF", "HFpEF"],
    # --- Commercial Batch 3 ---
    # Autoimmune orphan
    "sjogrens_syndrome": ["Sjogren", "Sjogrens", "sicca syndrome", "dry eye autoimmune",
                          "salivary gland inflammation", "primary Sjogren"],
    "myasthenia_gravis": ["MG", "myasthenia", "acetylcholine receptor antibody",
                          "neuromuscular junction", "anti-AChR"],
    "dermatomyositis": ["DM", "inflammatory myopathy", "heliotrope rash",
                        "Gottron papules", "anti-MDA5", "anti-Mi2"],
    "anca_vasculitis": ["ANCA vasculitis", "AAV", "anti-neutrophil cytoplasmic",
                        "pauci-immune", "ANCA-associated"],
    # GI / Hepatobiliary
    "autoimmune_hepatitis": ["AIH", "autoimmune liver disease", "lupoid hepatitis",
                             "autoimmune chronic hepatitis"],
    "alcoholic_hepatitis": ["alcohol hepatitis", "alcoholic liver disease", "ALD",
                            "alcohol-associated hepatitis", "ethanol hepatitis"],
    "celiac_disease": ["celiac", "coeliac", "gluten enteropathy", "celiac sprue",
                       "gluten-sensitive", "tissue transglutaminase"],
    # Hematologic
    "chronic_lymphocytic_leukemia": ["CLL", "B-cell chronic lymphocytic",
                                     "chronic lymphoid leukemia", "B-CLL", "SLL"],
    "mantle_cell_lymphoma": ["MCL", "mantle cell", "mantle zone lymphoma",
                             "cyclin D1 lymphoma"],
    "myelodysplastic_syndrome": ["MDS", "myelodysplasia", "refractory anemia",
                                 "refractory cytopenia", "preleukemia"],
    # Dermatology / Immune
    "hidradenitis_suppurativa": ["HS", "acne inversa", "hidradenitis",
                                 "suppurative hidradenitis", "apocrine gland"],
    "pemphigus": ["pemphigus vulgaris", "pemphigus foliaceus", "PV",
                  "desmoglein", "acantholysis", "autoimmune blistering"],
    "mycosis_fungoides": ["MF", "cutaneous T-cell lymphoma", "CTCL",
                          "Sezary syndrome", "mycosis fungoid"],
    # Blood / Immune
    "immune_thrombocytopenia": ["ITP", "immune thrombocytopenic purpura",
                                "autoimmune thrombocytopenia", "idiopathic thrombocytopenic"],
    "dlbcl": ["DLBCL", "diffuse large B-cell", "aggressive lymphoma",
              "non-Hodgkin lymphoma", "large B cell lymphoma"],
    "sjia": ["sJIA", "systemic JIA", "Still disease", "Stills disease",
             "juvenile idiopathic arthritis", "macrophage activation"],
    # Transplant / Muscle
    "graft_vs_host_disease": ["GVHD", "graft-versus-host", "graft versus host",
                              "allogeneic transplant rejection", "chronic GVHD", "acute GVHD"],
    "polymyositis": ["PM", "inflammatory myopathy", "polymyositis",
                     "anti-Jo1", "antisynthetase", "muscle inflammation"],
    # Emerging
    "sarcoidosis": ["sarcoid", "granulomatous disease", "Lofgren syndrome",
                    "noncaseating granuloma", "pulmonary sarcoidosis"],
    "chronic_rhinosinusitis_np": ["CRSwNP", "nasal polyps", "nasal polyposis",
                                  "chronic sinusitis", "eosinophilic sinusitis"],
    # --- Commercial Batch 4 ---
    # Autoimmune renal
    "lupus_nephritis": ["lupus nephritis", "lupus kidney", "lupus glomerulonephritis",
                        "class IV nephritis", "proliferative nephritis", "LN"],
    "membranous_nephropathy": ["membranous nephropathy", "membranous glomerulonephritis",
                               "PLA2R", "podocyte", "nephrotic syndrome"],
    "minimal_change_disease": ["minimal change disease", "MCD", "lipoid nephrosis",
                               "nephrotic syndrome", "podocyte effacement"],
    # Neuroinflammatory
    "nmosd": ["NMOSD", "neuromyelitis optica", "Devic disease", "aquaporin-4",
              "AQP4 antibody", "optic neuritis"],
    "autoimmune_encephalitis": ["autoimmune encephalitis", "anti-NMDAR encephalitis",
                                "NMDA receptor", "limbic encephalitis", "anti-LGI1",
                                "anti-NMDA receptor", "encephalitis autoimmune",
                                "paraneoplastic encephalitis", "anti-CASPR2", "brain inflammation"],
    # Hematologic non-solid
    "follicular_lymphoma": ["follicular lymphoma", "FL", "indolent lymphoma",
                            "follicle center lymphoma", "germinal center B-cell"],
    "waldenstroms": ["Waldenstrom", "WM", "lymphoplasmacytic lymphoma", "LPL",
                     "IgM paraprotein", "MYD88"],
    # Immune rare high-value
    "igg4_related_disease": ["IgG4", "IgG4-related", "IgG4-RD", "sclerosing disease",
                             "autoimmune pancreatitis", "Mikulicz disease"],
    "chronic_spontaneous_urticaria": ["CSU", "chronic urticaria", "chronic idiopathic urticaria",
                                      "spontaneous urticaria", "mast cell", "antihistamine-refractory"],
    "egpa": ["EGPA", "Churg-Strauss", "eosinophilic granulomatosis", "eosinophilic vasculitis",
             "hypereosinophilic", "allergic granulomatosis"],
    # GI + systemic
    "microscopic_colitis": ["microscopic colitis", "collagenous colitis", "lymphocytic colitis",
                            "watery diarrhea", "subepithelial collagen",
                            "MC colitis", "incomplete microscopic colitis",
                            "chronic watery diarrhea", "colonic inflammation"],
    "endometriosis": ["endometriosis", "endometrioma", "ectopic endometrium",
                      "endometrial lesion", "adenomyosis", "pelvic pain"],
    "adult_onset_stills_disease": ["AOSD", "adult Still", "Stills disease", "adult-onset Still",
                                   "quotidian fever", "ferritin", "macrophage activation"],
    # Hepatobiliary + blood rare
    "primary_sclerosing_cholangitis": ["PSC", "sclerosing cholangitis", "biliary stricture",
                                       "cholestatic", "bile duct inflammation"],
    "behcets_disease": ["Behcet", "Behcets", "Behcet syndrome", "oral ulcer vasculitis",
                        "mucocutaneous", "silk road disease"],
    "aplastic_anemia": ["aplastic anemia", "bone marrow failure", "pancytopenia",
                        "hypoplastic anemia", "immune-mediated aplastic",
                        "acquired aplastic anemia", "severe aplastic anemia",
                        "hematopoietic failure", "marrow aplasia", "SAA"],
    # Skin/immune + vasculitis
    "lichen_planus": ["lichen planus", "oral lichen planus", "lichenoid",
                      "cutaneous lichen planus", "mucosal lichen planus"],
    "iga_vasculitis": ["IgA vasculitis", "Henoch-Schonlein", "HSP", "Henoch Schonlein purpura",
                       "IgA immune complex", "leukocytoclastic vasculitis"],
    # --- Commercial Batch 5 (S-tier, zero approved drug) ---
    # Skin/mucosal autoimmune
    "lichen_sclerosus": ["lichen sclerosus", "vulvar lichen sclerosus", "BXO",
                         "balanitis xerotica obliterans", "genital lichen sclerosus",
                         "vulvar sclerosis", "vulvar dermatosis"],
    "morphea": ["morphea", "localized scleroderma", "localised scleroderma",
                "linear scleroderma", "circumscribed scleroderma", "skin fibrosis"],
    "cutaneous_lupus": ["CLE", "cutaneous lupus", "discoid lupus", "discoid lupus erythematosus",
                        "subacute cutaneous lupus", "SCLE", "DLE", "lupus skin"],
    "pyoderma_gangrenosum": ["pyoderma gangrenosum", "neutrophilic dermatosis",
                             "ulcerative dermatosis", "PG skin"],
    # Inflammatory myopathy
    "inclusion_body_myositis": ["IBM", "inclusion body myositis", "sporadic inclusion body myositis",
                                "inflammatory myopathy", "rimmed vacuole myopathy"],
    # GI immune
    "eosinophilic_gastroenteritis": ["EGE", "eosinophilic gastritis", "eosinophilic enteritis",
                                     "gastrointestinal eosinophilia", "eosinophilic GI disorder"],
    "autoimmune_gastritis": ["autoimmune gastritis", "autoimmune atrophic gastritis",
                             "type A gastritis", "pernicious anemia", "parietal cell antibody",
                             "atrophic gastritis autoimmune"],
    "pouchitis": ["pouchitis", "ileal pouch inflammation", "IPAA inflammation",
                  "ileoanal pouch", "j-pouch inflammation", "pouch colitis"],
    # Urological
    "interstitial_cystitis": ["interstitial cystitis", "bladder pain syndrome", "IC/BPS",
                              "painful bladder syndrome", "Hunner lesion", "Hunner ulcer"],
    # Autoimmune bullous
    "mucous_membrane_pemphigoid": ["MMP", "cicatricial pemphigoid", "mucous membrane pemphigoid",
                                   "ocular cicatricial pemphigoid", "benign mucous membrane pemphigoid"],
    # Autoimmune rare
    "autoimmune_pancreatitis": ["AIP", "autoimmune pancreatitis", "IgG4 pancreatitis",
                                "lymphoplasmacytic sclerosing pancreatitis", "type 1 AIP"],
    "relapsing_polychondritis": ["relapsing polychondritis", "chondritis", "auricular chondritis",
                                 "cartilage inflammation", "nasal chondritis"],
    # --- Commercial Batch 6 (S/A/B-tier, 7 therapeutic areas) ---
    # Autoimmune renal
    "fsgs": ["FSGS", "focal segmental glomerulosclerosis", "focal glomerulosclerosis",
             "podocyte injury", "nephrotic syndrome FSGS", "focal sclerosis glomerular"],
    "c3_glomerulopathy": ["C3 glomerulopathy", "C3GN", "C3 glomerulonephritis",
                          "dense deposit disease", "DDD", "complement glomerulonephritis",
                          "membranoproliferative glomerulonephritis complement"],
    # Autoimmune bullous
    "bullous_pemphigoid": ["bullous pemphigoid", "BP180", "BP230", "pemphigoid",
                           "subepidermal blistering", "autoimmune blistering skin"],
    "linear_iga_disease": ["linear IgA disease", "linear IgA dermatosis", "LAD",
                           "linear IgA bullous dermatosis", "chronic bullous disease childhood"],
    "epidermolysis_bullosa_acquisita": ["EBA", "epidermolysis bullosa acquisita",
                                        "acquired epidermolysis bullosa", "type VII collagen autoimmune",
                                        "mechanobullous disease"],
    # Vasculitis
    "granulomatosis_with_polyangiitis": ["GPA", "granulomatosis with polyangiitis",
                                         "Wegener granulomatosis", "Wegener's granulomatosis",
                                         "ANCA vasculitis granulomatous", "PR3 vasculitis"],
    "polyarteritis_nodosa": ["PAN", "polyarteritis nodosa", "panarteritis nodosa",
                              "periarteritis nodosa", "necrotizing arteritis medium vessel"],
    "takayasu_arteritis": ["Takayasu arteritis", "Takayasu's arteritis", "TAK",
                           "aortic arch syndrome", "pulseless disease", "aortitis"],
    # Neuro-immune
    "cidp": ["CIDP", "chronic inflammatory demyelinating polyneuropathy",
             "chronic inflammatory demyelinating polyradiculoneuropathy",
             "demyelinating neuropathy", "inflammatory neuropathy"],
    "autoimmune_hemolytic_anemia": ["AIHA", "autoimmune hemolytic anemia",
                                    "warm autoimmune hemolytic anemia", "cold agglutinin disease",
                                    "autoimmune hemolysis", "Coombs positive anemia"],
    # Endocrine/orbital immune
    "thyroid_eye_disease": ["TED", "thyroid eye disease", "Graves ophthalmopathy",
                            "Graves orbitopathy", "thyroid-associated orbitopathy", "TAO",
                            "thyroid associated ophthalmopathy"],
    "graves_disease": ["Graves disease", "Graves' disease", "Basedow disease",
                       "toxic diffuse goiter", "autoimmune hyperthyroidism",
                       "thyroid stimulating immunoglobulin"],
    # Rare hematologic/immune
    "systemic_mastocytosis": ["systemic mastocytosis", "SM", "mast cell disease",
                               "mastocytosis systemic", "KIT D816V", "mast cell neoplasm"],
    "castleman_disease": ["Castleman disease", "Castleman's disease", "MCD",
                          "multicentric Castleman", "angiofollicular lymph node hyperplasia",
                          "hyaline vascular Castleman"],
    # Large market
    "diabetic_nephropathy": ["diabetic nephropathy", "diabetic kidney disease", "DKD",
                              "diabetic glomerulosclerosis", "Kimmelstiel-Wilson",
                              "diabetes renal", "diabetic renal disease"],
    # --- Commercial Batch 7 (S/A/B-tier, 8 therapeutic areas) ---
    # Connective tissue / overlap
    "antisynthetase_syndrome": ["antisynthetase syndrome", "anti-Jo-1 syndrome", "anti-Jo1",
                                 "ASyS", "anti-synthetase", "inflammatory myopathy anti-Jo"],
    "mixed_connective_tissue_disease": ["MCTD", "mixed connective tissue disease",
                                         "Sharp syndrome", "overlap syndrome",
                                         "undifferentiated connective tissue disease"],
    # Fibrotic/scarring
    "keloid": ["keloid", "keloid scar", "hypertrophic scar", "keloid fibroblast",
               "pathological scarring", "keloid formation"],
    "oral_submucous_fibrosis": ["OSMF", "oral submucous fibrosis", "oral submucosal fibrosis",
                                 "submucous fibrosis", "betel nut fibrosis", "areca nut fibrosis"],
    # Hematologic autoimmune
    "antiphospholipid_syndrome": ["APS", "antiphospholipid syndrome", "lupus anticoagulant",
                                   "anticardiolipin", "anti-beta2 glycoprotein",
                                   "Hughes syndrome", "aPL antibody"],
    "evans_syndrome": ["Evans syndrome", "AIHA ITP", "autoimmune pancytopenia",
                       "immune bicytopenia", "Evans syndrome autoimmune"],
    "cryoglobulinemic_vasculitis": ["cryoglobulinemia", "cryoglobulinemic vasculitis",
                                    "mixed cryoglobulinemia", "cryoglobulin",
                                    "essential cryoglobulinemia", "HCV vasculitis"],
    # Endocrine autoimmune
    "hashimoto_thyroiditis": ["Hashimoto thyroiditis", "Hashimoto's thyroiditis",
                               "lymphocytic thyroiditis", "autoimmune thyroiditis",
                               "chronic lymphocytic thyroiditis", "Hashimoto disease"],
    "type_1_diabetes": ["T1D", "type 1 diabetes", "IDDM", "insulin dependent diabetes",
                        "autoimmune diabetes", "juvenile diabetes", "islet autoimmunity",
                        "beta cell autoimmune"],
    # Ophthalmologic
    "autoimmune_uveitis": ["autoimmune uveitis", "non-infectious uveitis", "uveitis",
                            "anterior uveitis", "panuveitis", "uveal inflammation",
                            "intraocular inflammation"],
    # Dermatologic
    "granuloma_annulare": ["granuloma annulare", "GA skin", "granulomatous dermatosis",
                           "palisading granuloma", "annular granuloma"],
    "prurigo_nodularis": ["prurigo nodularis", "PN", "nodular prurigo",
                          "chronic prurigo", "prurigo nodularis Hyde"],
    # GI / Obstetric
    "chronic_pancreatitis": ["chronic pancreatitis", "recurrent pancreatitis",
                              "calcific pancreatitis", "pancreatic fibrosis",
                              "chronic relapsing pancreatitis"],
    "preeclampsia": ["preeclampsia", "pre-eclampsia", "toxemia of pregnancy",
                     "gestational hypertension", "HELLP", "eclampsia",
                     "hypertensive disorder of pregnancy"],
    # --- Commercial Batch 7 expansion ---
    # Ophthalmologic / multi-system
    "vogt_koyanagi_harada": ["VKH", "Vogt-Koyanagi-Harada", "VKH disease",
                              "panuveitis melanocyte", "sympathetic ophthalmia",
                              "autoimmune melanocyte"],
    "dry_eye_disease": ["dry eye", "dry eye disease", "DED", "keratoconjunctivitis sicca",
                        "tear film dysfunction", "meibomian gland dysfunction",
                        "ocular surface disease"],
    # Fibrotic expansion
    "uterine_fibroids": ["uterine fibroid", "leiomyoma", "myoma uteri", "uterine myoma",
                          "fibroid uterus", "uterine leiomyoma"],
    # Dermatologic expansion
    "rosacea": ["rosacea", "acne rosacea", "erythematotelangiectatic rosacea",
                "papulopustular rosacea", "rhinophyma", "ocular rosacea"],
    "dermatitis_herpetiformis": ["dermatitis herpetiformis", "Duhring disease",
                                  "DH celiac skin", "IgA dermatitis",
                                  "gluten-sensitive enteropathy skin"],
    # Neuro-immune rare
    "stiff_person_syndrome": ["SPS", "stiff person syndrome", "stiff-man syndrome",
                               "GAD antibody syndrome", "anti-GAD65",
                               "autoimmune stiffness"],
    "lambert_eaton": ["LEMS", "Lambert-Eaton", "Lambert-Eaton myasthenic syndrome",
                      "VGCC antibody", "presynaptic neuromuscular junction"],
    # Oncology expansion
    "mesothelioma": ["mesothelioma", "malignant mesothelioma", "pleural mesothelioma",
                     "peritoneal mesothelioma", "MPM", "asbestos cancer"],
    "bladder_cancer": ["urothelial carcinoma", "bladder cancer", "transitional cell carcinoma",
                       "TCC bladder", "urothelial bladder cancer", "muscle-invasive bladder"],
    "endometrial_cancer": ["endometrial cancer", "endometrial carcinoma", "uterine cancer",
                           "endometrial adenocarcinoma", "uterine corpus cancer"],
    # --- Commercial Batch 8 ---
    # Musculoskeletal
    "osteoarthritis": ["osteoarthritis", "OA", "degenerative joint disease",
                       "osteoarthrosis", "knee osteoarthritis", "hip osteoarthritis",
                       "cartilage degeneration"],
    "psoriatic_arthritis": ["PsA", "psoriatic arthritis", "psoriatic arthropathy",
                             "arthritis psoriatic", "psoriasis arthritis"],
    "gout": ["gout", "gouty arthritis", "crystal arthropathy", "urate crystal",
             "tophaceous gout", "acute gout", "monosodium urate"],
    "polymyalgia_rheumatica": ["PMR", "polymyalgia rheumatica", "polymyalgia",
                                "rheumatic polymyalgia", "polymyalgia arteritic"],
    # Pulmonary immune
    "hypersensitivity_pneumonitis": ["HP", "hypersensitivity pneumonitis",
                                     "extrinsic allergic alveolitis", "EAA",
                                     "bird fancier lung", "farmer lung"],
    "lymphangioleiomyomatosis": ["LAM", "lymphangioleiomyomatosis",
                                  "lymphangiomyomatosis", "pulmonary LAM",
                                  "TSC LAM", "sporadic LAM"],
    # Neuro-immune
    "narcolepsy": ["narcolepsy", "narcolepsy type 1", "narcolepsy cataplexy",
                   "hypocretin deficiency", "orexin deficiency", "NT1"],
    "guillain_barre": ["GBS", "Guillain-Barre", "Guillain-Barré syndrome",
                       "acute inflammatory demyelinating polyneuropathy", "AIDP",
                       "ascending paralysis"],
    # Dermatologic
    "acne_vulgaris": ["acne", "acne vulgaris", "cystic acne", "inflammatory acne",
                      "Propionibacterium acnes", "Cutibacterium acnes", "comedone"],
    "sweet_syndrome": ["Sweet syndrome", "acute febrile neutrophilic dermatosis",
                       "Sweet's syndrome", "neutrophilic dermatosis acute"],
    # Renal autoimmune
    "anti_gbm_disease": ["anti-GBM", "Goodpasture syndrome", "Goodpasture disease",
                          "anti-glomerular basement membrane", "anti-GBM antibody",
                          "pulmonary-renal syndrome"],
    # Endocrine/reproductive
    "addisons_disease": ["Addison disease", "Addison's disease", "autoimmune adrenalitis",
                          "primary adrenal insufficiency", "adrenocortical insufficiency",
                          "autoimmune adrenal failure"],
    "pcos": ["PCOS", "polycystic ovary syndrome", "polycystic ovarian syndrome",
             "Stein-Leventhal syndrome", "ovarian hyperandrogenism",
             "polycystic ovaries"],
    # Cardiac inflammatory
    "pericarditis": ["pericarditis", "recurrent pericarditis", "acute pericarditis",
                     "constrictive pericarditis", "pericardial inflammation",
                     "idiopathic pericarditis"],
    "kawasaki_disease": ["Kawasaki disease", "Kawasaki syndrome",
                          "mucocutaneous lymph node syndrome", "MLNS",
                          "infantile polyarteritis", "Kawasaki vasculitis"],
    # Oncology expansion
    "sclc": ["SCLC", "small cell lung cancer", "small cell lung carcinoma",
             "small cell carcinoma lung", "neuroendocrine lung cancer",
             "oat cell carcinoma"],
    "cervical_cancer": ["cervical cancer", "cervical carcinoma",
                        "cervical squamous cell carcinoma", "HPV cervical",
                        "cervical intraepithelial neoplasia", "uterine cervix cancer"],
    "esophageal_cancer": ["esophageal cancer", "esophageal carcinoma",
                          "oesophageal cancer", "esophageal squamous cell carcinoma",
                          "esophageal adenocarcinoma", "Barrett esophagus cancer"],
    "thyroid_cancer": ["thyroid cancer", "thyroid carcinoma",
                       "papillary thyroid carcinoma", "PTC",
                       "follicular thyroid carcinoma", "anaplastic thyroid cancer",
                       "differentiated thyroid cancer"],
    # --- Commercial Batch 9 (S-tier sweep) ---
    # Pulmonary
    "chronic_eosinophilic_pneumonia": ["chronic eosinophilic pneumonia", "CEP",
                                       "eosinophilic pneumonia chronic",
                                       "Carrington disease", "pulmonary eosinophilia"],
    "pulmonary_alveolar_proteinosis": ["PAP", "pulmonary alveolar proteinosis",
                                        "alveolar proteinosis", "GM-CSF autoantibody",
                                        "autoimmune PAP", "whole lung lavage"],
    "cryptogenic_organizing_pneumonia": ["COP", "cryptogenic organizing pneumonia",
                                          "organizing pneumonia", "BOOP",
                                          "bronchiolitis obliterans organizing pneumonia"],
    "bronchiolitis_obliterans": ["bronchiolitis obliterans", "obliterative bronchiolitis",
                                  "constrictive bronchiolitis", "BOS",
                                  "small airway fibrosis", "popcorn lung"],
    # Fibrotic
    "retroperitoneal_fibrosis": ["retroperitoneal fibrosis", "RPF", "Ormond disease",
                                  "periaortitis", "IgG4 retroperitoneal",
                                  "idiopathic retroperitoneal fibrosis"],
    # Auto-inflammatory
    "schnitzler_syndrome": ["Schnitzler syndrome", "Schnitzler's syndrome",
                             "chronic urticaria monoclonal IgM",
                             "urticarial vasculitis IgM", "Schnitzler"],
    "sapho_syndrome": ["SAPHO", "SAPHO syndrome", "synovitis acne pustulosis",
                       "hyperostosis osteitis", "sterile osteomyelitis",
                       "chronic recurrent multifocal osteomyelitis"],
    # Dermatologic
    "necrobiosis_lipoidica": ["necrobiosis lipoidica", "necrobiosis lipoidica diabeticorum",
                               "NLD", "granulomatous dermatitis necrobiosis"],
    "panniculitis": ["panniculitis", "lobular panniculitis", "erythema nodosum",
                     "Weber-Christian disease", "subcutaneous inflammation",
                     "fat necrosis panniculitis"],
    # Sensory autoimmune
    "autoimmune_inner_ear_disease": ["AIED", "autoimmune inner ear disease",
                                      "autoimmune sensorineural hearing loss",
                                      "immune-mediated hearing loss",
                                      "autoimmune labyrinthitis"],
    # Hematologic rare
    "hypereosinophilic_syndrome": ["HES", "hypereosinophilic syndrome",
                                    "idiopathic hypereosinophilia",
                                    "eosinophilic disorder", "hypereosinophilia",
                                    "FIP1L1-PDGFRA negative HES"],
    "poems_syndrome": ["POEMS", "POEMS syndrome", "osteosclerotic myeloma",
                       "Crow-Fukase syndrome", "polyneuropathy organomegaly",
                       "Takatsuki syndrome"],
    # --- Commercial Batch 11 (beyond autoimmunity: high-burden inflammatory) ---
    # Critical care
    "sepsis": ["sepsis", "septic shock", "severe sepsis", "bacteremia",
               "systemic inflammatory response", "SIRS", "septicemia",
               "immune paralysis", "immunosuppression sepsis"],
    # Pulmonary inflammation (non-autoimmune)
    "cystic_fibrosis": ["cystic fibrosis", "CF lung", "CFTR", "CF airway",
                        "CF sputum", "cystic fibrosis lung", "CF exacerbation",
                        "mucoviscidosis", "CF bronchiectasis"],
    "bronchiectasis": ["bronchiectasis", "non-CF bronchiectasis", "airway dilation",
                       "chronic airway infection", "bronchiectatic",
                       "cylindrical bronchiectasis", "saccular bronchiectasis"],
    # Metabolic inflammation
    "obesity": ["obesity", "obese", "adipose tissue", "visceral fat",
                "adipose inflammation", "metabolic inflammation",
                "insulin resistance", "adipose macrophage", "BMI"],
    # Neuropsychiatric inflammation
    "major_depressive_disorder": ["MDD", "major depression", "major depressive disorder",
                                  "depressive disorder", "unipolar depression",
                                  "treatment-resistant depression", "TRD",
                                  "neuroinflammation depression"],
    "chronic_fatigue_syndrome": ["CFS", "ME/CFS", "myalgic encephalomyelitis",
                                 "chronic fatigue syndrome", "post-exertional malaise",
                                 "systemic exertion intolerance disease", "SEID"],
    # Transplant immunology
    "kidney_transplant_rejection": ["kidney transplant rejection", "renal allograft rejection",
                                    "kidney allograft", "renal transplant",
                                    "T-cell mediated rejection", "antibody-mediated rejection",
                                    "Banff classification", "allograft nephropathy"],
    # Infectious immune / host-directed
    "chronic_hepatitis_c": ["hepatitis C", "chronic hepatitis C", "HCV",
                            "HCV infection", "chronic HCV", "hepatitis C virus",
                            "HCV chronic", "CHC", "post-SVR inflammation"],
    "tuberculosis": ["tuberculosis", "pulmonary tuberculosis", "TB",
                     "Mycobacterium tuberculosis", "MTB", "TB lung",
                     "TB granuloma", "latent TB", "active tuberculosis"],
    # Common dermatology
    "seborrheic_dermatitis": ["seborrheic dermatitis", "seborrheic eczema",
                              "seborrhoeic dermatitis", "dandruff",
                              "Malassezia dermatitis", "sebopsoriasis",
                              "cradle cap", "facial dermatitis"],
    # Protein misfolding immune
    "systemic_amyloidosis": ["AL amyloidosis", "amyloidosis", "light chain amyloidosis",
                             "immunoglobulin light chain amyloidosis", "systemic amyloidosis",
                             "amyloid deposit", "primary amyloidosis"],
    # Hematologic inflammation
    "sickle_cell_disease": ["sickle cell", "sickle cell disease", "SCD",
                            "sickle cell anemia", "HbSS", "vaso-occlusive crisis",
                            "sickle cell crisis", "hemoglobin SS disease"],
    # --- Commercial Batch 11 A-tier expansion ---
    # Allergy / hypersensitivity
    "allergic_rhinitis": ["allergic rhinitis", "hay fever", "seasonal allergic rhinitis",
                          "perennial rhinitis", "nasal allergy", "rhinoconjunctivitis",
                          "allergic nasal", "nasal mucosa allergy", "nasal epithelium allergic",
                          "pollen allergy", "house dust mite rhinitis", "AR nasal",
                          "nasal lavage allergic", "inferior turbinate"],
    "food_allergy": ["food allergy", "peanut allergy", "food hypersensitivity",
                     "IgE food allergy", "oral allergy syndrome", "food anaphylaxis",
                     "milk allergy", "egg allergy", "food allergen", "food allergic",
                     "tree nut allergy", "shellfish allergy", "wheat allergy",
                     "food challenge", "oral immunotherapy", "food sensitization"],
    "abpa": ["ABPA", "allergic bronchopulmonary aspergillosis",
             "bronchopulmonary aspergillosis", "Aspergillus hypersensitivity",
             "allergic aspergillosis", "mucoid impaction Aspergillus",
             "Aspergillus fumigatus allergy", "Aspergillus bronchial",
             "ABPA asthma", "Aspergillus sensitization",
             "fungal allergy lung", "eosinophilic Aspergillus"],
    # Transplant immunology expansion
    "kidney_transplant_rejection": ["kidney transplant rejection", "renal allograft rejection",
                                    "kidney allograft", "renal transplant",
                                    "T-cell mediated rejection", "antibody-mediated rejection",
                                    "Banff classification", "allograft nephropathy",
                                    "renal graft rejection", "kidney biopsy rejection",
                                    "TCMR kidney", "ABMR kidney", "protocol biopsy renal",
                                    "transplant nephrectomy", "chronic allograft nephropathy"],
    "liver_transplant_rejection": ["liver transplant rejection", "hepatic allograft rejection",
                                   "liver allograft", "liver transplant",
                                   "hepatic graft rejection", "acute cellular rejection liver",
                                   "antibody-mediated rejection liver",
                                   "liver biopsy rejection", "TCMR liver",
                                   "orthotopic liver transplant", "OLT rejection",
                                   "hepatic graft dysfunction", "liver transplant biopsy",
                                   "chronic rejection liver"],
    "heart_transplant_rejection": ["heart transplant rejection", "cardiac allograft rejection",
                                   "cardiac transplant", "heart allograft",
                                   "endomyocardial biopsy rejection", "cardiac graft rejection",
                                   "acute cellular rejection heart",
                                   "EMB rejection", "heart biopsy rejection",
                                   "cardiac allograft vasculopathy", "CAV transplant",
                                   "TCMR heart", "ABMR heart", "cardiac transplant biopsy"],
    "lung_transplant_rejection": ["lung transplant rejection", "pulmonary allograft rejection",
                                  "lung allograft", "lung transplant",
                                  "chronic lung allograft dysfunction", "CLAD",
                                  "acute rejection lung transplant",
                                  "BOS lung transplant", "bronchiolitis obliterans syndrome transplant",
                                  "RAS lung transplant", "lung biopsy rejection",
                                  "BAL lung transplant", "transbronchial biopsy rejection",
                                  "lung graft rejection"],
    # Acute immune
    "ards": ["ARDS", "acute respiratory distress syndrome", "acute lung injury",
             "ALI", "diffuse alveolar damage", "DAD",
             "respiratory failure immune", "ARDS lung", "ALI lung",
             "ARDS BAL", "ventilator lung injury", "pulmonary edema acute",
             "neutrophilic alveolitis", "lung injury acute", "ARDS sepsis"],
    "secondary_hlh": ["HLH", "hemophagocytic lymphohistiocytosis",
                      "hemophagocytic syndrome", "macrophage activation syndrome",
                      "MAS", "secondary HLH", "reactive hemophagocytic",
                      "cytokine storm", "hyperferritinemia", "hemophagocytosis",
                      "HLH blood", "HLH bone marrow", "sJIA MAS", "familial HLH"],
    # Rheumatologic / autoinflammatory
    "polyarticular_jia": ["polyarticular JIA", "juvenile idiopathic arthritis",
                          "juvenile rheumatoid arthritis", "JIA polyarticular",
                          "RF-positive JIA", "RF-negative JIA", "pediatric arthritis",
                          "JIA synovial", "JIA PBMC", "juvenile arthritis",
                          "oligoarticular JIA", "JIA flare", "childhood arthritis"],
    "reactive_arthritis": ["reactive arthritis", "Reiter syndrome", "Reiter's syndrome",
                           "post-infectious arthritis", "HLA-B27 arthritis",
                           "urogenital reactive arthritis", "enteric reactive arthritis",
                           "ReA arthritis", "Chlamydia arthritis", "Salmonella arthritis",
                           "spondyloarthritis reactive", "post-dysenteric arthritis"],
    "cppd_disease": ["CPPD", "calcium pyrophosphate", "pseudogout",
                     "chondrocalcinosis", "pyrophosphate arthropathy",
                     "calcium pyrophosphate deposition", "CPPD crystal",
                     "CPP crystal", "calcium pyrophosphate dihydrate",
                     "CPPD synovial", "pseudogout synovial fluid",
                     "crystal arthritis pyrophosphate", "articular chondrocalcinosis"],
    "fmf": ["FMF", "familial Mediterranean fever", "MEFV",
            "Mediterranean fever", "recurrent polyserositis",
            "autoinflammatory fever", "familial paroxysmal polyserositis",
            "MEFV mutation", "FMF blood", "FMF PBMC",
            "familial periodic fever", "pyrin inflammasome",
            "colchicine-resistant FMF", "FMF amyloidosis"],
    # Neuro-immune
    "mogad": ["MOGAD", "MOG antibody", "MOG-IgG", "MOG antibody disease",
              "anti-MOG", "MOG-associated disorder",
              "myelin oligodendrocyte glycoprotein antibody",
              "MOG encephalomyelitis", "MOG optic neuritis", "MOG-AD",
              "MOG spectrum disorder", "anti-MOG positive",
              "MOG demyelination", "ADEM MOG"],
    # ENT immune
    "chronic_sinusitis_snp": ["CRSsNP", "chronic sinusitis without polyps",
                              "chronic rhinosinusitis without nasal polyps",
                              "non-polypoid sinusitis", "neutrophilic sinusitis",
                              "chronic sinusitis", "chronic rhinosinusitis",
                              "sinus mucosa inflammation", "ethmoid sinus chronic",
                              "maxillary sinus chronic", "sinusitis biopsy",
                              "CRS non-eosinophilic", "sinus tissue inflammation"],
    # Reproductive immune
    "chronic_endometritis": ["chronic endometritis", "endometritis",
                             "endometrial inflammation", "CD138 endometrium",
                             "plasma cell endometritis", "uterine inflammation",
                             "endometrial plasma cell", "endometrial biopsy inflammation",
                             "chronic uterine inflammation", "endometritis infertility",
                             "endometrial immune", "recurrent implantation failure endometritis",
                             "CE endometrium"],
    # Hematologic autoimmune
    "ttp": ["TTP", "thrombotic thrombocytopenic purpura",
            "acquired TTP", "autoimmune TTP", "ADAMTS13 deficiency",
            "anti-ADAMTS13", "thrombotic microangiopathy TTP",
            "TTP blood", "TTP PBMC", "TTP plasma",
            "thrombotic microangiopathy", "TMA immune",
            "ADAMTS13 antibody", "iTTP"],
    # --- Commercial Batch 12 (A-tier+: all ≥ A-tier per SAB framework) ---
    # Myeloproliferative / Hematologic
    "polycythemia_vera": ["polycythemia vera", "PV", "polycythaemia vera",
                          "JAK2 V617F", "JAK2 myeloproliferative",
                          "erythrocytosis", "primary polycythemia",
                          "myeloproliferative neoplasm PV", "PV blood",
                          "PV bone marrow", "post-PV myelofibrosis"],
    "essential_thrombocythemia": ["essential thrombocythemia", "ET",
                                  "essential thrombocytosis", "primary thrombocythemia",
                                  "JAK2 thrombocythemia", "CALR mutation ET",
                                  "MPL mutation", "megakaryocyte proliferation",
                                  "ET bone marrow", "ET blood", "platelet disorder MPN"],
    "cmml": ["CMML", "chronic myelomonocytic leukemia", "chronic myelomonocytic leukaemia",
             "myelodysplastic myeloproliferative", "MDS/MPN",
             "monocytosis CMML", "CMML bone marrow", "CMML blood",
             "chronic myelomonocytic", "MDS MPN overlap"],
    "al_amyloidosis": ["AL amyloidosis", "light chain amyloidosis",
                       "immunoglobulin light chain amyloidosis", "primary amyloidosis",
                       "systemic AL amyloidosis", "amyloid deposit",
                       "cardiac amyloidosis AL", "renal amyloidosis",
                       "lambda light chain", "kappa light chain amyloidosis"],
    "peripheral_tcell_lymphoma": ["PTCL", "peripheral T-cell lymphoma",
                                  "PTCL-NOS", "T-cell lymphoma",
                                  "angioimmunoblastic T-cell lymphoma", "AITL",
                                  "anaplastic large cell lymphoma", "ALCL",
                                  "adult T-cell lymphoma", "T-cell NHL",
                                  "mature T-cell neoplasm"],
    "marginal_zone_lymphoma": ["MZL", "marginal zone lymphoma",
                               "MALT lymphoma", "mucosa-associated lymphoid tissue",
                               "splenic marginal zone lymphoma", "SMZL",
                               "nodal marginal zone lymphoma", "NMZL",
                               "extranodal marginal zone", "gastric MALT"],
    # Complement / hemoglobinopathy
    "pnh": ["PNH", "paroxysmal nocturnal hemoglobinuria",
            "paroxysmal nocturnal haemoglobinuria",
            "complement-mediated hemolysis PNH", "GPI anchor deficiency",
            "CD55 CD59 deficiency", "PNH clone", "PNH blood",
            "hemolytic PNH", "nocturnal hemoglobinuria",
            "PNH granulocyte", "aplastic anemia PNH"],
    # (sickle_cell_disease already in batch 11 EXTRA_KEYWORDS)
    # Neuro-immune / autonomic
    "mecfs": ["ME/CFS", "myalgic encephalomyelitis", "chronic fatigue syndrome",
              "CFS", "post-exertional malaise", "PEM",
              "systemic exertion intolerance disease", "SEID",
              "chronic fatigue immune dysfunction", "CFIDS",
              "ME CFS blood", "ME CFS PBMC", "neuroimmune fatigue"],
    "pots": ["POTS", "postural orthostatic tachycardia syndrome",
             "postural tachycardia syndrome", "orthostatic intolerance",
             "autonomic dysfunction", "dysautonomia",
             "autoimmune POTS", "neuropathic POTS",
             "adrenergic receptor antibody", "POTS blood",
             "tilt table", "orthostatic tachycardia"],
    # Pulmonary immune
    "ctd_ild": ["CTD-ILD", "connective tissue disease interstitial lung disease",
                "autoimmune ILD", "myositis-associated ILD", "DM-ILD",
                "RA-ILD", "rheumatoid lung", "Sjogren ILD",
                "antisynthetase ILD", "connective tissue disease lung fibrosis",
                "autoimmune interstitial pneumonia", "NSIP autoimmune"],
    # Dermatologic
    "palmoplantar_pustulosis": ["palmoplantar pustulosis", "PPP",
                                "pustulosis palmaris et plantaris",
                                "palmoplantar pustular psoriasis",
                                "pustular psoriasis palmoplantar",
                                "IL-36 pustulosis", "acrodermatitis continua",
                                "PPP skin biopsy", "palmoplantar pustular"],
    # Ophthalmologic
    "scleritis": ["scleritis", "anterior scleritis", "posterior scleritis",
                  "necrotizing scleritis", "diffuse scleritis",
                  "nodular scleritis", "scleral inflammation",
                  "autoimmune scleritis", "ocular inflammation scleritis"],
    # GI immune
    "eosinophilic_colitis": ["eosinophilic colitis", "EC", "colonic eosinophilia",
                             "eosinophilic colon", "eosinophilic GI disease colon",
                             "eosinophilic gastrointestinal colitis",
                             "primary eosinophilic colitis", "tissue eosinophilia colon"],
    # Rare immune / histiocytic
    "langerhans_cell_histiocytosis": ["LCH", "Langerhans cell histiocytosis",
                                      "histiocytosis X", "eosinophilic granuloma",
                                      "Hand-Schuller-Christian", "Letterer-Siwe",
                                      "BRAF V600E histiocytosis", "LCH bone",
                                      "LCH skin", "pulmonary LCH", "PLCH"],
    "cold_agglutinin_disease": ["cold agglutinin disease", "CAD",
                                "cold agglutinin", "cold autoimmune hemolytic anemia",
                                "cold AIHA", "cold hemagglutinin disease",
                                "IgM hemolytic anemia", "complement-mediated hemolysis",
                                "cold agglutinin blood", "cold agglutinin titer"],
    "vexas_syndrome": ["VEXAS", "VEXAS syndrome", "UBA1 mutation",
                       "vacuoles E1 enzyme X-linked autoinflammatory somatic",
                       "UBA1 somatic mutation", "VEXAS blood", "VEXAS bone marrow",
                       "autoinflammatory UBA1", "myeloid VEXAS",
                       "VEXAS MDS", "UBA1 myeloid"],
    # Autoinflammatory
    "traps": ["TRAPS", "TNF receptor-associated periodic syndrome",
              "TNFRSF1A", "tumor necrosis factor receptor periodic fever",
              "familial Hibernian fever", "TRAPS blood", "TRAPS PBMC",
              "periodic fever TNFRSF1A", "autoinflammatory TRAPS",
              "TNFRSF1A mutation"],
    # --- Commercial Batch 13 (A-tier+: infection HDT, ICI toxicity, rare heme/autoimmune) ---
    # Infection immune / HDT
    "leprosy": ["leprosy", "Hansen disease", "Hansen's disease",
                "Mycobacterium leprae", "lepromatous leprosy", "tuberculoid leprosy",
                "erythema nodosum leprosum", "ENL", "type 1 reaction leprosy",
                "reversal reaction leprosy", "borderline leprosy",
                "leprosy skin biopsy", "leprosy nerve", "multibacillary"],
    "cerebral_malaria": ["cerebral malaria", "severe malaria", "Plasmodium falciparum",
                         "malaria brain", "severe P. falciparum", "CM malaria",
                         "malaria blood transcriptome", "malaria PBMC",
                         "malaria immune", "complicated malaria",
                         "malaria endothelial", "sequestration malaria"],
    "chagas_cardiomyopathy": ["Chagas cardiomyopathy", "Chagas disease", "Chagas heart",
                              "Trypanosoma cruzi", "chagasic cardiomyopathy",
                              "chronic Chagas", "T. cruzi cardiac",
                              "Chagas myocarditis", "Chagas fibrosis",
                              "American trypanosomiasis cardiac"],
    "schistosomiasis_liver": ["hepatic schistosomiasis", "schistosomiasis", "Schistosoma",
                              "schistosomal liver fibrosis", "Schistosoma mansoni liver",
                              "Schistosoma japonicum liver", "granulomatous liver disease",
                              "periportal fibrosis schistosomiasis",
                              "schistosomal granuloma", "egg granuloma liver"],
    "chronic_hepatitis_d": ["hepatitis D", "chronic hepatitis D", "HDV",
                            "hepatitis delta", "delta virus", "HDV infection",
                            "hepatitis D virus", "HDV RNA", "HDV superinfection",
                            "delta hepatitis", "HDV chronic"],
    # Immune checkpoint inhibitor toxicity
    "ici_colitis": ["checkpoint inhibitor colitis", "immune-related colitis",
                    "irAE colitis", "ipilimumab colitis", "nivolumab colitis",
                    "immune checkpoint colitis", "ICI colitis",
                    "immune-mediated diarrhea", "checkpoint diarrhea",
                    "anti-CTLA4 colitis", "anti-PD1 colitis", "irColitis"],
    "ici_myocarditis": ["checkpoint inhibitor myocarditis", "immune-related myocarditis",
                        "irAE myocarditis", "ICI myocarditis",
                        "immune checkpoint myocarditis", "immune-mediated myocarditis",
                        "nivolumab myocarditis", "pembrolizumab myocarditis",
                        "checkpoint cardiotoxicity", "irMyocarditis"],
    # Rare hematologic
    "hairy_cell_leukemia": ["hairy cell leukemia", "HCL", "hairy cell",
                            "leukemic reticuloendotheliosis", "HCL variant",
                            "BRAF V600E leukemia", "hairy cell bone marrow",
                            "tartrate-resistant acid phosphatase", "TRAP stain HCL"],
    "t_lgl_leukemia": ["T-LGL leukemia", "T-cell large granular lymphocyte leukemia",
                       "LGL leukemia", "large granular lymphocyte",
                       "T-LGL lymphoproliferative", "T-LGL blood",
                       "CD3+CD8+ LGL", "STAT3 LGL", "NK-LGL",
                       "granular lymphocyte leukemia"],
    "pure_red_cell_aplasia": ["PRCA", "pure red cell aplasia",
                              "acquired pure red cell aplasia",
                              "erythroid aplasia", "red cell aplasia",
                              "anti-erythropoietin antibody",
                              "thymoma PRCA", "PRCA bone marrow"],
    "alps": ["ALPS", "autoimmune lymphoproliferative syndrome",
             "Canale-Smith syndrome", "FAS deficiency",
             "lymphoproliferative syndrome autoimmune",
             "double negative T cell", "DNT cell",
             "FAS mutation lymphoproliferative", "ALPS blood"],
    "ptld": ["PTLD", "post-transplant lymphoproliferative disorder",
             "post-transplant lymphoproliferative disease",
             "EBV lymphoproliferative", "EBV PTLD",
             "transplant lymphoma", "post-transplant EBV",
             "PTLD tissue", "polymorphic PTLD", "monomorphic PTLD"],
    # Rare autoimmune myopathy
    "necrotizing_autoimmune_myopathy": ["IMNM", "immune-mediated necrotizing myopathy",
                                        "necrotizing autoimmune myopathy", "NAM",
                                        "anti-SRP myopathy", "anti-HMGCR myopathy",
                                        "necrotizing myopathy", "statin myopathy autoimmune",
                                        "necrotizing myositis", "IMNM muscle biopsy"],
    "eosinophilic_fasciitis": ["eosinophilic fasciitis", "Shulman syndrome",
                               "diffuse fasciitis with eosinophilia",
                               "fasciitis eosinophilic", "deep fascia inflammation",
                               "EF fasciitis", "fascial fibrosis eosinophilic",
                               "eosinophilic fasciitis skin biopsy"],
    # Genetic inflammatory
    "alpha1_antitrypsin_deficiency": ["alpha-1 antitrypsin deficiency", "AATD", "A1AT",
                                      "alpha-1 antitrypsin", "AAT deficiency",
                                      "SERPINA1", "PiZZ", "ZZ genotype",
                                      "alpha1-proteinase inhibitor deficiency",
                                      "emphysema alpha-1", "liver disease alpha-1"],
    # Transplant / vascular
    "hepatic_vod": ["hepatic VOD", "veno-occlusive disease", "sinusoidal obstruction syndrome",
                    "SOS liver", "VOD/SOS", "hepatic sinusoidal obstruction",
                    "transplant VOD", "VOD liver biopsy",
                    "defibrotide", "hepatic veno-occlusive"],
    # Immune deficiency paradox
    "cvid_granulomatous": ["CVID", "common variable immunodeficiency",
                           "CVID granulomatous", "GLILD",
                           "granulomatous lymphocytic interstitial lung disease",
                           "CVID autoimmune", "CVID lymphoproliferative",
                           "CVID PBMC", "CVID blood", "hypogammaglobulinemia CVID"],
    # Post-infectious GI immune
    "post_infectious_ibs": ["post-infectious IBS", "PI-IBS",
                            "post-infectious irritable bowel syndrome",
                            "post-enteric IBS", "IBS mucosal inflammation",
                            "IBS mast cell", "IBS T cell infiltration",
                            "post-gastroenteritis IBS", "IBS biopsy inflammation"],
    # Neuro-immune
    "anti_mag_neuropathy": ["anti-MAG neuropathy", "anti-MAG", "MAG antibody neuropathy",
                            "IgM paraproteinemic neuropathy", "IgM neuropathy",
                            "myelin-associated glycoprotein neuropathy",
                            "demyelinating paraproteinemic neuropathy",
                            "anti-MAG IgM", "DADS neuropathy",
                            "distal acquired demyelinating symmetric"],
}


def fetch_opentargets_synonyms(efo_id: str, timeout: float = 10.0) -> list[str]:
    """Fetch disease name + synonyms from OpenTargets GraphQL API."""
    query = """
    query DiseaseInfo($efoId: String!) {
      disease(efoId: $efoId) {
        name
        synonyms { terms }
      }
    }
    """
    payload = json.dumps({"query": query, "variables": {"efoId": efo_id}}).encode()
    req = Request(
        "https://api.platform.opentargets.org/api/v4/graphql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        disease = data.get("data", {}).get("disease")
        if not disease:
            return []
        terms = []
        if disease.get("name"):
            terms.append(disease["name"])
        for syn_group in disease.get("synonyms", []) or []:
            for t in syn_group.get("terms", []) or []:
                if t and t not in terms:
                    terms.append(t)
        return terms
    except Exception as e:
        logger.warning("OpenTargets synonym fetch failed for %s: %s", efo_id, e)
        return []


def _clean_keywords(raw_terms: list[str], disease_name: str, max_keywords: int = 12) -> list[str]:
    """Deduplicate, filter overly long/generic terms, and limit count."""
    seen = set()
    keywords = []
    # Always include the disease name first
    for term in [disease_name] + raw_terms:
        normalized = term.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        # Skip very long synonyms (> 60 chars) — they won't match sample metadata
        if len(normalized) > 60:
            continue
        # Skip very short terms (< 3 chars) — too ambiguous (AD, PE, etc.)
        if len(normalized) < 3:
            continue
        # Skip terms that are just IDs (e.g., "OMIM:123456")
        if re.match(r'^[A-Z]+:\d+$', normalized):
            continue
        seen.add(key)
        keywords.append(normalized)
        if len(keywords) >= max_keywords:
            break
    return keywords


def build_keywords(disease_key: str, disease_name: str, efo_id: str) -> list[str]:
    """Build case keywords: OpenTargets synonyms + extra abbreviations."""
    # 1. Fetch synonyms from OpenTargets
    ot_terms = fetch_opentargets_synonyms(efo_id)
    if ot_terms:
        logger.info("OpenTargets synonyms for %s (%s): %d terms", disease_key, efo_id, len(ot_terms))
    else:
        logger.warning("No OpenTargets synonyms for %s, using disease name only", disease_key)
        ot_terms = [disease_name]

    # 2. Append extra abbreviations/colloquial terms
    extras = EXTRA_KEYWORDS.get(disease_key, [])

    # 3. Clean and deduplicate
    return _clean_keywords(ot_terms + extras, disease_name)


def generate_config(disease_key: str, disease_name: str, efo_id: str,
                    h5_path: str = "data/archs4/human_gene_v2.5.h5",
                    case_keywords: list[str] | None = None) -> dict:
    """Generate a config dict for a single disease."""
    if case_keywords is None:
        case_keywords = build_keywords(disease_key, disease_name, efo_id)

    config = {
        "project": {
            "name": f"{disease_key}_archs4_signature",
            "outdir": f"outputs/{disease_key}",
            "workdir": f"work/{disease_key}",
            "seed": 42,
        },
        "disease": {
            "name": disease_name,
            "efo_id": efo_id,
        },
        "archs4": {
            "h5_path": h5_path,
            "min_samples_per_group": 3,
            "max_samples_per_group": 50,
            "max_series": 5,
            "case_keywords": case_keywords,
            "control_keywords": ["normal", "healthy", "control"],
        },
        "opentargets": {
            "min_association_score": 0.1,
        },
        "de": {
            "method": "deseq2",
            "min_count": 10,
            "min_samples": 3,
        },
        "meta": {
            "model": "DL",
            "min_sign_concordance": 0.8,
            "flag_i2_above": 0.75,
        },
        "signature": {
            "top_n": 300,
            "weight_formula": "meta_z_times_ot_score_times_1minusFDR",
        },
    }
    return config


def parse_disease_list(list_path: str) -> list[dict]:
    """Parse disease list file (same format as ops/disease_list*.txt)."""
    diseases = []
    with open(list_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                logger.warning("Skipping invalid line: %s", line)
                continue
            disease_key = parts[0].strip()
            disease_name = parts[1].strip() if parts[1].strip() else disease_key.replace("_", " ")
            efo_id = parts[2].strip() if len(parts) > 2 else ""
            if not efo_id:
                logger.warning("No EFO ID for %s, skipping", disease_key)
                continue
            diseases.append({
                "key": disease_key,
                "name": disease_name,
                "efo_id": efo_id,
            })
    return diseases


def main():
    ap = argparse.ArgumentParser(description="Auto-generate ARCHS4 pipeline configs")
    ap.add_argument("--disease-list", help="Path to disease list file")
    ap.add_argument("--disease", help="Single disease key")
    ap.add_argument("--disease-name", help="Disease display name (with --disease)")
    ap.add_argument("--efo-id", help="EFO ID (with --disease)")
    ap.add_argument("--h5-path", default="data/archs4/human_gene_v2.5.h5",
                    help="Path to ARCHS4 H5 file")
    ap.add_argument("--outdir", default="archs4_signature_pipeline/configs",
                    help="Output directory for config files")
    args = ap.parse_args()

    configs_dir = Path(args.outdir)
    configs_dir.mkdir(parents=True, exist_ok=True)

    diseases = []

    if args.disease_list:
        diseases = parse_disease_list(args.disease_list)
    elif args.disease:
        if not args.efo_id:
            logger.error("--efo-id required with --disease")
            raise SystemExit(1)
        name = args.disease_name or args.disease.replace("_", " ")
        diseases = [{"key": args.disease, "name": name, "efo_id": args.efo_id}]
    else:
        logger.error("Either --disease-list or --disease required")
        raise SystemExit(1)

    for d in diseases:
        cfg = generate_config(d["key"], d["name"], d["efo_id"], h5_path=args.h5_path)
        out_path = configs_dir / f"{d['key']}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info("Generated: %s (%s, %s) — %d keywords",
                     out_path, d["name"], d["efo_id"], len(cfg["archs4"]["case_keywords"]))

    logger.info("Generated %d config files in %s", len(diseases), configs_dir)


if __name__ == "__main__":
    main()
