import logging
import pandas as pd
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# EEOC four-fifths rule: a selection rate ratio below 0.8 is presumptive evidence
# of disparate impact under U.S. employment guidelines.
DEFAULT_DI_THRESHOLD = 0.8

# Below this per-group sample size, disparate impact ratios are statistical noise.
DEFAULT_MIN_GROUP_SIZE = 30

# Which recommendation labels count as "selected" for the four-fifths analysis.
DEFAULT_SELECTED_LABELS = frozenset({"hire", "strong_hire"})


class BiasAuditor:

    def __init__(self):
        pass

    def audit_cohort(
            self,
            candidates_df: pd.DataFrame,
            protected_attribute: str = 'gender',
            threshold: float = DEFAULT_DI_THRESHOLD,
            min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
            selected_labels: Optional[Set[str]] = None,
    ) -> Dict:

        if protected_attribute not in candidates_df.columns:
            return {'error': f'Protected attribute {protected_attribute} not found'}

        selected_labels = set(selected_labels) if selected_labels else set(DEFAULT_SELECTED_LABELS)

        # Drop rows with NaN in the protected attribute — NaN as a "group" is meaningless
        nan_count = int(candidates_df[protected_attribute].isna().sum())
        df = candidates_df.dropna(subset=[protected_attribute])
        if nan_count:
            logger.warning("Dropped %d rows with missing %s", nan_count, protected_attribute)

        groups = df[protected_attribute].unique()

        if len(groups) < 2:
            return {'error': 'Need at least 2 non-null groups for comparison'}

        selection_rates = {}
        group_counts = {}
        undersized_groups = []

        for group in groups:
            group_data = df[df[protected_attribute] == group]
            selected = group_data[group_data['recommendation'].isin(selected_labels)]

            selection_rate = len(selected) / len(group_data) if len(group_data) > 0 else 0
            selection_rates[group] = selection_rate
            group_counts[group] = len(group_data)
            if len(group_data) < min_group_size:
                undersized_groups.append(group)

        max_rate = max(selection_rates.values())
        min_rate = min(selection_rates.values())

        disparate_impact_ratio = min_rate / max_rate if max_rate > 0 else 0
        passes_disparate_impact = disparate_impact_ratio >= threshold

        stat_parity_diff = max_rate - min_rate

        score_by_group = {}
        for group in groups:
            group_scores = df[df[protected_attribute] == group]['overall_score'].dropna()
            n = len(group_scores)
            score_by_group[group] = {
                'n': int(n),
                'mean': float(group_scores.mean()) if n > 0 else 0.0,
                'median': float(group_scores.median()) if n > 0 else 0.0,
                # Sample stdev is undefined for n<2; report 0.0 rather than NaN (NaN breaks JSON)
                'std': float(group_scores.std()) if n > 1 else 0.0,
            }

        low_confidence = bool(undersized_groups)

        return {
            'protected_attribute': protected_attribute,
            'groups_analyzed': list(groups),
            'group_counts': group_counts,
            'dropped_missing_attribute': nan_count,
            'selected_labels': sorted(selected_labels),
            'selection_rates': selection_rates,
            'disparate_impact_ratio': round(disparate_impact_ratio, 3),
            'passes_disparate_impact': passes_disparate_impact,
            'threshold_used': threshold,
            'statistical_parity_diff': round(stat_parity_diff, 3),
            'score_distributions': score_by_group,
            'low_confidence': low_confidence,
            'undersized_groups': undersized_groups,
            'min_group_size': min_group_size,
            'overall_pass': passes_disparate_impact and stat_parity_diff < 0.15 and not low_confidence,
            'recommendations': self._generate_recommendations(
                disparate_impact_ratio,
                stat_parity_diff,
                threshold,
                undersized_groups,
                min_group_size,
            )
        }

    def _generate_recommendations(
            self,
            di_ratio: float,
            sp_diff: float,
            threshold: float,
            undersized_groups: Optional[List] = None,
            min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
    ) -> List[str]:
        recommendations = []
        undersized_groups = undersized_groups or []

        if undersized_groups:
            recommendations.append(
                f"⚠️ LOW CONFIDENCE: groups {undersized_groups} have fewer than "
                f"{min_group_size} candidates — disparate impact ratio is unreliable"
            )

        if di_ratio < threshold:
            recommendations.append(
                f"⚠️ DISPARATE IMPACT DETECTED: Ratio {di_ratio:.2f} below {threshold} threshold"
            )
            recommendations.append(
                "Consider reviewing question selection and scoring criteria"
            )

        if sp_diff > 0.15:
            recommendations.append(
                f"⚠️ STATISTICAL PARITY GAP: {sp_diff:.1%} difference in selection rates"
            )
            recommendations.append(
                "Review for potential systematic bias in evaluation"
            )

        if not recommendations:
            recommendations.append("✓ No significant bias detected in current cohort")

        return recommendations
