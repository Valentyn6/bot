# Фітнес-бот константи для валідації та розрахунків

# ============ Вікові обмеження ============
MIN_AGE = 13
MAX_AGE = 120

# ============ Зроста обмеження (см) ============
MIN_HEIGHT_CM = 100
MAX_HEIGHT_CM = 250

# ============ Ваги обмеження (кг) ============
MIN_WEIGHT_KG = 20
MAX_WEIGHT_KG = 300

# ============ Калорійних обмеження ============
MAX_PROTEIN_PER_KG = 1000
MAX_FAT_PER_KG = 1000
MAX_CARBS_PER_KG = 1000
MAX_CALORIES_PER_PRODUCT = 10000
MIN_REALISTIC_CALORIES = 200  # мінімум калорій за день для реальності
MAX_REALISTIC_CALORIES = 10000  # максимум калорій за день для реальності

# ============ Активність обмеження (ккал) ============
MIN_ACTIVITY_KCAL = 0
MAX_ACTIVITY_KCAL = 10000  # максимум за день

# ============ Одиниці виміру ============
VALID_FOOD_UNITS = {"г", "мл", "шт"}
DEFAULT_FOOD_UNIT = "г"
DEFAULT_PROFILE_HEIGHT = 177
DEFAULT_PROFILE_AGE = 19
DEFAULT_PROFILE_SEX = "чоловіча"
DEFAULT_PROFILE_WEIGHT = 71  # дефолт при відсутності даних
DEFAULT_ACTIVITY_KCAL = 0

# ============ Макроелементи дефолти ============
DEFAULT_PROTEIN_PER_KG = 2.0
DEFAULT_FAT_PER_KG = 1.0
DEFAULT_SURPLUS_DEFICIT = 0
DEFAULT_SURPLUS_GAIN = 300  # додаткові ккал при наборі
DEFAULT_SURPLUS_MAINTENANCE = 0
DEFAULT_DEFICIT_LOSS = 400  # відняти ккал при схуднені

# ============ BMR (Basal Metabolic Rate) константи ============
# Формула Харріс-Бенедикта
BMR_MALE_CONSTANT = 5  # чоловіча стала
BMR_FEMALE_CONSTANT = -161  # жіноча стала

# ============ Мінімальні значення для обчислень ============
MIN_FOOD_PROTEIN = 0
MAX_FOOD_PROTEIN = 100
MIN_FOOD_FAT = 0
MAX_FOOD_FAT = 100
MIN_FOOD_CARBS = 0
MAX_FOOD_CARBS = 100

# ============ Темпові файли ============
TEMP_FILE_PREFIX = "fitness_"
TEMP_FILE_SUFFIX = ".png"
CHART_DPI = 150
CHART_FIGSIZE = (9, 4.5)

# ============ Вживання щодо рекомендацій ============
MIN_PROTEIN_REMAINING_FOR_ADVICE = 10  # г
MIN_CARBS_REMAINING_FOR_ADVICE = 20  # г
MIN_CALORIES_REMAINING_FOR_ADVICE = 100  # ккал
SIMILARITY_SCORE_THRESHOLD = 0.45  # мінімум для пошуку продуктів

# ============ Режими мета ============
GOAL_MODES = {"підтримання", "набір", "схуднення"}
DEFAULT_GOAL_MODE = "підтримання"

# ============ Сексі ============
VALID_SEXES = {"чоловіча", "жіноча"}

# ============ Статуси доступу ============
ACCESS_STATUSES = {"approved", "pending", "rejected", "banned"}
DEFAULT_ACCESS_STATUS = "pending"

# ============ Обмеження для食物 аналітики ============
WEEKLY_ACTIVITY_DAYS = 7
MAX_FOOD_NAME_LENGTH = 255

# ============ Обмеження сну ============
MAX_SLEEP_DURATION_HOURS = 14  # максимум годин сну за ніч
MIN_SLEEP_DURATION_HOURS = 2  # мінімум годин сну за ніч

# ============ Логування ============
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
LOG_LEVEL = "INFO"
LOG_FILE = "fitness_bot.log"
LOG_MAX_BYTES = int(1e7)  # 10 MB
LOG_BACKUP_COUNT = 5
