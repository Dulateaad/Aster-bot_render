-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    status ENUM('pending', 'approved', 'rejected') DEFAULT 'pending',
    name VARCHAR(255),
    phone VARCHAR(50),
    city VARCHAR(100),
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    cheque_file_id VARCHAR(255)
);

-- Таблица объявлений
CREATE TABLE IF NOT EXISTS ads (
    ad_id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    model VARCHAR(255),
    year INT,
    price INT,
    description TEXT,
    photos JSON,
    inspection_photos JSON,
    thickness_photos JSON,
    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица избранных объявлений
CREATE TABLE IF NOT EXISTS favorites (
    user_id BIGINT,
    ad_id INT,
    PRIMARY KEY (user_id, ad_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (ad_id) REFERENCES ads(ad_id) ON DELETE CASCADE
);

-- Таблица подписок
CREATE TABLE IF NOT EXISTS subscriptions (
    rowid INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT,
    model VARCHAR(255),
    price_min INT,
    price_max INT,
    year_min INT,
    year_max INT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Таблица настроек бота
CREATE TABLE IF NOT EXISTS bot_settings (
    `key` VARCHAR(50) PRIMARY KEY,
    `value` VARCHAR(50)
);