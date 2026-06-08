library(tidyverse)
library(patchwork)
library(lubridate)

csv_path <- "C:\\Users\\reub0539\\work\\Planet_LSP\\data\\wytham_test\\metadata\\clipped_scene_catalogue.csv"

df <- read_csv(csv_path, show_col_types = FALSE) %>%
  mutate(
    satellite_id = as.factor(satellite_id),
    acquired_dt = ymd_hms(acquired, tz = "UTC"),
    across(
      c(mean_blue, mean_green, mean_red, mean_nir,
        blue_nir_ratio, ndvi_mean, ndvi_sd,
        clear_percent, cloud_cover, valid_pixel_count),
      as.numeric
    )
  ) %>%
  filter(!is.na(satellite_id), !is.na(acquired_dt))

colour_metrics <- c(
  "mean_blue",
  "mean_green",
  "mean_red",
  "mean_nir",
  "blue_nir_ratio",
  "ndvi_mean"
)

df_long <- df %>%
  pivot_longer(
    cols = all_of(colour_metrics),
    names_to = "metric",
    values_to = "value"
  )

# 1) Colour metrics by satellite_id
p_box <- ggplot(df_long, aes(x = satellite_id, y = value, fill = satellite_id)) +
  geom_boxplot(outlier.alpha = 0.3) +
  facet_wrap(~metric, scales = "free_y", ncol = 2) +
  labs(title = "Colour metrics by satellite_id", x = "satellite_id", y = "Value") +
  theme_minimal(base_size = 12) +
  theme(legend.position = "none", axis.text.x = element_text(angle = 45, hjust = 1))

print(p_box)

# 2) Colour metrics over time, coloured by satellite_id
# ------------------------------------------------------------
# Colour metrics over time, split by polygon
# ------------------------------------------------------------

polygon_ids <- unique(df_long$polygon_id)

for (poly in polygon_ids) {

  p_time <- df_long %>%
    filter(polygon_id == poly) %>%
    ggplot(
      aes(
        x = acquired_dt,
        y = value,
        colour = satellite_id
      )
    ) +
    geom_point(alpha = 0.6, size = 1.5) +
    facet_wrap(~metric, scales = "free_y", ncol = 2) +
    labs(
      title = paste("Colour metrics over time:", poly),
      x = "Acquisition time",
      y = "Value",
      colour = "satellite_id"
    ) +
    theme_minimal(base_size = 12)

  print(p_time)

  ggsave(
    paste0("colour_metrics_over_time_", poly, ".png"),
    p_time,
    width = 14,
    height = 10,
    dpi = 300
  )
}

# 3) clear_percent over time
p_clear_time <- ggplot(df, aes(x = acquired_dt, y = clear_percent, colour = satellite_id)) +
  geom_point(alpha = 0.6) +
  labs(
    title = "Clear percent over time",
    x = "Acquisition time",
    y = "clear_percent",
    colour = "satellite_id"
  ) +
  theme_minimal(base_size = 12)

print(p_clear_time)

# 4) cloud_cover over time
p_cloud_time <- ggplot(df, aes(x = acquired_dt, y = cloud_cover, colour = satellite_id)) +
  geom_point(alpha = 0.6) +
  labs(
    title = "Cloud cover over time",
    x = "Acquisition time",
    y = "cloud_cover",
    colour = "satellite_id"
  ) +
  theme_minimal(base_size = 12)

print(p_cloud_time)

# Optional save
ggsave("colour_metrics_by_satellite.png", p_box, width = 14, height = 10, dpi = 300)
ggsave("colour_metrics_over_time.png", p_time, width = 14, height = 10, dpi = 300)
ggsave("clear_percent_over_time.png", p_clear_time, width = 12, height = 6, dpi = 300)
ggsave("cloud_cover_over_time.png", p_cloud_time, width = 12, height = 6, dpi = 300)