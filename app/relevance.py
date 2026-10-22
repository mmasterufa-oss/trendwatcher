"""Проверка, что ролик про мебель: ищем мебельные слова в описании и речи."""
import re

FURNITURE = re.compile(
    r"мебел|мебельщ|диван|софа|тахт|шкаф|шкафч|купе\b|гардероб|кухн|кухон|гарнитур|столешниц|"
    r"\bстол|столик|стол[аеуы]\b|стул|табурет|кресл|пуф|банкетк|кроват|матрас|изголов|комод|тумб|"
    r"полк|полоч|стеллаж|этажерк|прихож|обувниц|вешалк|гостин|спальн|детск(ая|ую|ой) комнат|"
    r"двухъярус|чердак|интерьер|корпусн|фасад|фурнитур|лдсп|мдф|массив|сборк|собира|собрал|"
    r"икеа|ikea|furniture|sofa|couch|wardrobe|closet|dresser|cabinet|kitchen|table|chair|"
    r"\bbed\b|bedroom|shelf|shelves|bookcase|interior|makeover|nursery",
    re.I,
)
# явно не наша тема — снижаем, если нет сильных мебельных слов
OFF = re.compile(r"недвижим|квартир[аы] от|ипотек|застройщ|психолог|отношени|похуд|макияж|маникюр", re.I)


def score(post: dict) -> int:
    text = f"{post.get('caption') or ''} {post.get('speech') or ''}"
    hits = len(FURNITURE.findall(text))
    if hits and OFF.search(text) and hits < 3:
        return 0
    return hits


def is_relevant(post: dict) -> bool:
    return score(post) > 0
