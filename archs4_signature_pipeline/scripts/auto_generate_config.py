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
