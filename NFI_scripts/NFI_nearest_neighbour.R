# script for lookign at distance to nearest forest neighbour from the GB NFI filtered to only looking at deciduous woodlands
# we want to see how close forests are to their nearest neighbour to get an idea how many are 

# libraries 
library(sf)
library(ggplot2)
library(tidyverse)

# create figures folder if it doesn't exist
if (!dir.exists("figures")) {
    dir.create("figures")
}

# load data 
NFI_forests <- read.csv("data/nearest_neighbour.csv")
NFI_forests_joined <- read.csv("data/nearest_neighbour_joined.csv")

head(NFI_forests)
head(NFI_forests_joined)


# create df with just ID and distance between them
NFI_nearest <- NFI_forests  %>% 
    select(FID, FID_2, distance)

NFI_nearest_joined <- NFI_forests_joined  %>% 
    select(fid, fid_2, distance)

# remove self-matches and mirrored pairs so each neighbour relationship appears once
NFI_nearest_unique <- NFI_nearest %>%
    filter(FID != FID_2) %>%
    mutate(pair_key = paste(pmin(FID, FID_2), pmax(FID, FID_2), sep = "_")) %>%
    distinct(pair_key, .keep_all = TRUE) %>%
    select(-pair_key)

head(NFI_nearest_unique)


# remove self-matches and mirrored pairs so each neighbour relationship appears once
NFI_nearest_joined_unique <- NFI_nearest_joined %>%
    filter(fid != fid_2) %>%
    mutate(pair_key = paste(pmin(fid, fid_2), pmax(fid, fid_2), sep = "_")) %>%
    distinct(pair_key, .keep_all = TRUE) %>%
    select(-pair_key)

head(NFI_nearest_joined_unique)

# plot a histogram showing the distance to the nearest neighbour
# with mirrored IDs removed (e.g. 1-10 and 10-1 are treated as the same pair)

p1 <- ggplot(NFI_nearest_unique, aes(distance)) +
    geom_histogram(bins = 40, color = "black", fill = "steelblue") +
    scale_x_log10(
        breaks = scales::trans_breaks("log10", function(x) 10^x),
        labels = scales::label_comma(accuracy = 1)
    ) +
    labs(
        title = "Distance to Nearest Forest Neighbour (log10 scale)",
        x = "Distance (log10 scale)",
        y = "Count"
    ) +
    theme_minimal() +
    theme(
        plot.title = element_text(size = 16, face = "bold"),
        axis.title = element_text(size = 14),
        axis.text = element_text(size = 12)
    )

ggsave("figures/distance_histogram_log10.png", p1, width = 8, height = 6, dpi = 300)


p2 <- ggplot(NFI_nearest_joined_unique, aes(distance)) +
    geom_histogram(bins = 40, color = "black", fill = "steelblue") +
    scale_x_log10(
        breaks = scales::trans_breaks("log10", function(x) 10^x),
        labels = scales::label_comma(accuracy = 1)
    ) +
    labs(
        title = "Distance to Nearest Forest Neighbour (log10 scale)",
        x = "Distance (log10 scale)",
        y = "Count"
    ) +
    theme_minimal() +
    theme(
        plot.title = element_text(size = 16, face = "bold"),
        axis.title = element_text(size = 14),
        axis.text = element_text(size = 12)
    )

ggsave("figures/distance_joined_histogram_log10.png", p2, width = 8, height = 6, dpi = 300)
